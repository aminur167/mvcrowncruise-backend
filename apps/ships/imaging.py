"""Derived image URLs.

Room photos are uploaded at whatever size the phone took them — routinely a
megabyte or more. That is right for the lightbox and wrong for a hover preview,
where a dozen of them may be fetched while someone sweeps a mouse across a deck
plan.

Public media lives on Cloudinary, whose URLs carry their transformations in the
path, so a thumbnail needs no extra storage, no upload step and no database
column: insert the transform after `/upload/` and the CDN renders and caches it.

Anything that is not a Cloudinary URL — local filesystem in development and in
tests — is returned untouched. A smaller file is an optimisation, and an
optimisation that only works in production is still correct in development.
"""

import re

#: Fill a 320x220 box, let Cloudinary pick the quality and the format (WebP/AVIF
#: where the browser supports it). ~15KB in practice, against ~1MB for the
#: original.
THUMBNAIL_SPEC = "w_320,h_220,c_fill,g_auto,q_auto,f_auto"

_CLOUDINARY_UPLOAD = re.compile(r"^(https?://res\.cloudinary\.com/[^/]+/image/upload/)")


def thumbnail_url(url, spec=THUMBNAIL_SPEC):
    """A small, CDN-rendered version of `url`, or `url` itself when it is not
    a Cloudinary image."""
    if not url:
        return url
    match = _CLOUDINARY_UPLOAD.match(url)
    if not match:
        return url
    prefix = match.group(1)
    rest = url[len(prefix) :]
    # Don't stack transforms if one is somehow already present.
    if rest.startswith(f"{spec}/"):
        return url
    return f"{prefix}{spec}/{rest}"
