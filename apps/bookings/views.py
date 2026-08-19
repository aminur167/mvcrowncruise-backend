import logging

from django.conf import settings
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from . import invoice_access, payment_service, sslcommerz
from .identity import normalize_booking_code, phone_last4_matches
from .models import Booking, Invoice, Payment
from .serializers import (
    BookingCreateSerializer,
    BookingInvoiceSerializer,
    BookingLookupSerializer,
    BookingPublicSerializer,
    BookingQuoteSerializer,
    PaymentInitiateSerializer,
    breakdown_as_json,
)

logger = logging.getLogger(__name__)


class BookingViewSet(
    mixins.CreateModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    queryset = Booking.objects.select_related("package").prefetch_related(
        "rooms__room__room_type"
    )
    serializer_class = BookingPublicSerializer
    lookup_field = "booking_code"
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "booking"

    def get_throttles(self):
        # Per-action buckets so cheap reads never drain the strict
        # booking-creation budget (default "booking", 10/min):
        #   quote           — fired on every pax change in the wizard.
        #   retrieve        — the post-payment status poll (every 2s ×~6) plus
        #     confirmation-page reads; a 429 here shows a just-paid customer a
        #     stuck state, so it gets its own generous "status" bucket
        #     (QA phase8b F1).
        #   invoices        — read-only list of the customer's own invoices.
        # create and pay are mutations and keep the strict "booking" scope.
        if self.action == "quote":
            self.throttle_scope = "quote"
        elif self.action in ("retrieve", "invoices", "cancellation_preview"):
            self.throttle_scope = "status"
        elif self.action == "lookup":
            # "Find my booking" is the one public endpoint that takes a code
            # the caller typed rather than one we handed them, so it is the
            # brute-force surface. Its own tight bucket, separate from the
            # generous poll budget.
            self.throttle_scope = "lookup"
        elif self.action == "cancellation_request":
            self.throttle_scope = "cancellation"
        return super().get_throttles()

    def get_object(self):
        """Look up by booking code, tolerating how people actually type it.

        Codes are read off a phone screen and re-typed into a form: lowercase,
        spaces, a missing "BK-". Postgres matching is exact, so without this
        every one of those is a 404 on a booking that plainly exists — the
        single most likely support call the lookup page would generate.
        """
        lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
        if lookup_url_kwarg in self.kwargs:
            self.kwargs[lookup_url_kwarg] = normalize_booking_code(
                self.kwargs[lookup_url_kwarg]
            )
        return super().get_object()

    def get_serializer_class(self):
        if self.action == "create":
            return BookingCreateSerializer
        if self.action == "quote":
            return BookingQuoteSerializer
        return BookingPublicSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        booking = serializer.save()
        data = BookingPublicSerializer(booking).data
        data["price_breakdown"] = breakdown_as_json(serializer.get_breakdown())
        return Response(data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"])
    def quote(self, request):
        """Price preview — validates like create but writes nothing."""
        serializer = BookingQuoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(breakdown_as_json(serializer.get_breakdown()))

    @action(detail=True, methods=["get"])
    def invoices(self, request, booking_code=None):
        """The customer's own invoices for this booking.

        Authorised by the booking_code itself, which is how every other public
        booking endpoint works (it is unguessable and only the customer has
        it). Each entry carries a download_url bearing the invoice's own
        capability token — so knowing one booking's code never reveals another
        customer's invoice (QA C1). Previously the customer had no way at all
        to re-obtain their invoice; the UI's "Download Receipt" button just
        opened a browser print dialog on an HTML card.
        """
        booking = self.get_object()
        invoices = booking.invoices.exclude(pdf_file="").order_by("-created_at")
        return Response(
            BookingInvoiceSerializer(
                invoices, many=True, context={"request": request}
            ).data
        )

    @action(detail=True, methods=["post"])
    def pay(self, request, booking_code=None):
        """Start an SSLCommerz checkout session (full or partial payment)."""
        booking = self.get_object()
        serializer = PaymentInitiateSerializer(
            data=request.data, context={"booking": booking}
        )
        serializer.is_valid(raise_exception=True)
        payment, gateway_url = payment_service.initiate_payment(
            booking, **serializer.validated_data
        )
        return Response(
            {
                "gateway_url": gateway_url,
                "tran_id": payment.transaction_id,
                "amount": str(payment.amount),
                "payment_type": payment.payment_type,
            }
        )

    @action(detail=False, methods=["post"])
    def lookup(self, request):
        """"Find my booking" — the public manage-booking entry point.

        The confirmation page is reached from the customer's own email link and
        needs the code alone. This form is different: it is a box on the website
        that anyone can type into, so it asks for the code AND the last four
        digits of the booking's phone number. A code that leaks in a forwarded
        email or a WhatsApp screenshot then no longer opens the booking on its
        own (see apps.bookings.identity).

        Both failure modes answer identically, so the endpoint never confirms
        that a code exists to a caller who cannot pass the second factor.
        """
        serializer = BookingLookupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        code = serializer.validated_data["booking_code"]
        booking = self.get_queryset().filter(booking_code=code).first()
        if booking is None or not phone_last4_matches(
            booking, serializer.validated_data["phone_last4"]
        ):
            return Response(
                {
                    "detail": (
                        "We could not find a booking with that code and phone "
                        "number. Please check both and try again."
                    )
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(BookingPublicSerializer(booking).data)

    @action(detail=True, methods=["get"], url_path="cancellation-preview")
    def cancellation_preview(self, request, booking_code=None):
        """What cancelling this booking would cost, before committing to it.

        Read-only and writes nothing, so it stays on the booking code alone —
        it reveals no more than the confirmation page the customer already has.
        The signed quote token it returns is what the submit endpoint checks the
        figures against.
        """
        from apps.refunds import policy, tokens
        from apps.refunds.serializers import (
            CancellationQuoteSerializer,
            CancellationRequestPublicSerializer,
        )

        booking = self.get_object()
        quote = policy.quote_cancellation(booking)
        payload = CancellationQuoteSerializer(quote).data
        payload["quote_token"] = tokens.issue(booking, quote) if quote.allowed else None
        payload["refund_sla_days"] = booking.package.ship.refund_sla_days
        pending = policy.pending_request_for(booking)
        payload["pending_request"] = (
            CancellationRequestPublicSerializer(pending).data if pending else None
        )
        return Response(payload)

    @action(detail=True, methods=["post"], url_path="cancellation-request")
    def cancellation_request(self, request, booking_code=None):
        """Ask us to cancel this booking.

        Creates a REQUEST — the booking keeps its status and its cabins until a
        human approves. That is what makes it safe to expose without accounts:
        the worst a stranger holding a leaked code (and the phone digits) can do
        is put an item in a queue someone reads.

        A booking with nothing paid is cancelled immediately instead: there is
        no money decision to make, and parking a dead hold in a queue keeps a
        sellable cabin off the market for no reason.
        """
        from django.core.exceptions import ValidationError as DjangoValidationError

        from apps.refunds import policy, services, tokens
        from apps.refunds.serializers import (
            CancellationQuoteSerializer,
            CancellationRequestCreateSerializer,
            CancellationRequestPublicSerializer,
        )

        booking = self.get_object()
        serializer = CancellationRequestCreateSerializer(
            data=request.data, context={"booking": booking}
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        quote = policy.quote_cancellation(booking)
        if not quote.allowed:
            return Response(
                {
                    "detail": "This booking can no longer be cancelled online.",
                    "block_reason": quote.block_reason,
                    "quote": CancellationQuoteSerializer(quote).data,
                },
                status=status.HTTP_409_CONFLICT,
            )
        try:
            tokens.verify(data["quote_token"], booking, quote)
        except tokens.QuoteTokenError as exc:
            # The numbers moved between preview and submit (a tier boundary
            # crossed, or the schedule was edited). Refuse and hand back the
            # fresh quote so the customer re-confirms against the real amount
            # instead of being charged something they never saw.
            return Response(
                {
                    "detail": str(exc),
                    "quote": CancellationQuoteSerializer(quote).data,
                    "quote_token": tokens.issue(booking, quote),
                },
                status=status.HTTP_409_CONFLICT,
            )

        try:
            cancellation = services.create_cancellation_request(
                booking,
                reason_code=data["reason_code"],
                reason_note=data.get("reason_note", ""),
                refund_method=data["refund_method"],
                refund_account_name=data["refund_account_name"],
                refund_account_number=data["refund_account_number"],
                bank_name=data.get("bank_name", ""),
                branch_name=data.get("branch_name", ""),
            )
        except DjangoValidationError as exc:
            return Response(
                exc.message_dict if hasattr(exc, "message_dict") else {"detail": str(exc)},
                status=status.HTTP_409_CONFLICT,
            )

        return Response(
            CancellationRequestPublicSerializer(cancellation).data,
            status=status.HTTP_201_CREATED,
        )


class InvoiceDownloadView(APIView):
    """Serve an invoice PDF against a signed, expiring download token.

    History: the PDF was once a raw MEDIA_URL link served by
    django.views.static.serve with no access check at all, at a path derivable
    from the booking code plus a sequential integer — so any customer could
    enumerate everyone else's invoices (QA C1). That was replaced by the
    invoice's permanent 256-bit access_token in the URL.

    Now the URL carries a signed token that dies in 30 minutes, and the
    permanent one never leaves the server (see apps.bookings.invoice_access for
    why a never-expiring link to a document full of personal data is worth
    closing). Links are minted per request by the booking's own invoices
    endpoint, which is already authorised by the booking code.
    """

    def get(self, request, token):
        try:
            invoice_id = invoice_access.resolve_id(token)
        except invoice_access.InvoiceLinkExpired:
            # The holder did nothing wrong — a real link simply aged out — so
            # say so instead of pretending the invoice does not exist.
            return Response(
                {
                    "detail": (
                        "This download link has expired. Open your booking "
                        "again to get a fresh one."
                    ),
                    "code": "link_expired",
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        if invoice_id is None:
            # Forged or mangled: the same blank 404 as a token for an invoice
            # that was never there.
            raise Http404("No such invoice.")

        invoice = get_object_or_404(Invoice, pk=invoice_id)
        if not invoice.pdf_file:
            raise Http404("This invoice has no PDF.")
        return invoice_access.harden(
            FileResponse(
                invoice.pdf_file.open("rb"),
                content_type="application/pdf",
                # Download it rather than rendering it in a tab, which would
                # park the token in the address bar and the browser history.
                as_attachment=True,
                filename=f"{invoice.number}.pdf",
            )
        )


def _frontend_redirect(result, request):
    """302 to the frontend result page. The page must fetch the booking by
    code for the real status — redirect data is presentation-only."""
    payment = None
    tran_id = request.data.get("tran_id")
    if tran_id:
        payment = Payment.objects.filter(transaction_id=tran_id).first()
    booking_code = payment.booking.booking_code if payment else ""
    return redirect(f"{settings.FRONTEND_URL}/payment/{result}?booking={booking_code}")


class PaymentIPNView(APIView):
    """SSLCommerz server-to-server notification.

    Two independent defences:
    - Crediting: the POSTed data is only a trigger — the verdict comes from
      the authenticated Validation API call inside process_payment_result().
    - State-closing: a FAILED/CANCELLED status is only honoured when the
      IPN's verify_sign/verify_key hash proves it came from SSLCommerz.
      Without this, anyone who learns a tran_id (it appears in redirect URLs
      and browser history — it is not a secret) could kill a live payment
      session and strand the customer's in-flight money.

    Signature-verified notifications are answered 200 so the gateway stops
    retrying; unverified ones get 400 and change nothing.

    Throttling is deliberately disabled: the default anon throttle keys on a
    spoofable header, so an attacker could 429 genuine gateway IPNs and delay
    payment confirmations. Signature verification replaces rate limiting here.
    """

    throttle_classes = []

    def post(self, request):
        if not sslcommerz.verify_ipn_signature(request.data):
            logger.warning(
                "Rejected IPN with missing/invalid signature (tran_id=%s)",
                request.data.get("tran_id"),
            )
            return Response(
                {"detail": "Invalid IPN signature."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        tran_id = request.data.get("tran_id")
        val_id = request.data.get("val_id")
        ipn_status = request.data.get("status")

        try:
            if ipn_status in ("FAILED",):
                payment_service.mark_payment_closed(tran_id, Payment.Status.FAILED)
            elif ipn_status in ("CANCELLED",):
                payment_service.mark_payment_closed(tran_id, Payment.Status.CANCELLED)
            else:
                payment_service.process_payment_result(tran_id, val_id)
        except Exception:
            # Fail SAFE, not closed. An unexpected error here (a DB constraint,
            # a bad row, anything) must never 500 back to SSLCommerz: the
            # gateway would retry the same poisoned notification indefinitely
            # while the customer's money sits captured and uncredited (QA C7).
            # Escalate to a human instead and answer 200 so the retry storm
            # stops — the payment stays PENDING and the reconciliation job and
            # the staff review queue both still see it.
            payment_service.flag_payment_for_review(
                tran_id, "IPN processing failed"
            )
            logger.exception(
                "IPN processing failed for %s — flagged for manual review", tran_id
            )
        return Response({"detail": "ok"})


class PaymentSuccessView(APIView):
    """Browser lands here after paying. Runs the same idempotent processing
    as the IPN (this is the path that settles payments in local dev, where
    the IPN can't reach localhost), then hands off to the frontend."""

    def post(self, request):
        payment_service.process_payment_result(
            request.data.get("tran_id"), request.data.get("val_id")
        )
        return _frontend_redirect("success", request)


class PaymentFailView(APIView):
    """Browser lands here when the gateway reports a failed attempt. The
    redirect is attacker-controllable (a plain POST with any tran_id), so it
    never closes a payment by itself — the gateway is asked what actually
    happened and only its answer changes state. The default anon throttle
    stays on: each hit costs an outbound gateway call."""

    def post(self, request):
        payment_service.close_payment_from_redirect(request.data.get("tran_id"))
        return _frontend_redirect("fail", request)


class PaymentCancelView(APIView):
    """Same trust model as PaymentFailView — presentation-first, state only
    on the gateway's confirmed answer."""

    def post(self, request):
        payment_service.close_payment_from_redirect(request.data.get("tran_id"))
        return _frontend_redirect("cancel", request)
