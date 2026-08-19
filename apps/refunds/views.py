"""Public read of the cancellation policy.

The customer-facing cancellation ACTIONS live on BookingViewSet, because they
are authorised by the booking code and belong with the rest of the booking's
public surface. What is left here is the schedule itself: the policy page used
to print a hardcoded array of tiers, which could silently drift from the table
the backend actually charges from. Now the page reads the same rows the money
comes out of.
"""

from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.ships.models import Ship

from . import policy
from .serializers import CancellationRuleSerializer


class CancellationPolicyView(APIView):
    """GET /api/cancellation-policy/?ship=<id>

    Omit `ship` and the first active ship answers — Phase 1 runs one ship, but
    the parameter is there so a second ship with its own schedule needs no new
    endpoint (and no frontend rewrite).
    """

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "read"

    def get(self, request):
        ship_id = request.query_params.get("ship")
        ships = Ship.objects.filter(status=Ship.Status.ACTIVE).order_by("id")
        ship = ships.filter(pk=ship_id).first() if ship_id else ships.first()
        if ship is None:
            return Response(
                {"detail": "No active ship configured."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(
            {
                "ship": ship.name,
                "refund_sla_days": ship.refund_sla_days,
                "tiers": CancellationRuleSerializer(
                    policy.describe_schedule(ship), many=True
                ).data,
            }
        )
