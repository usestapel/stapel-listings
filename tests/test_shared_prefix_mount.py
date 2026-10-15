"""A sibling module mounted under the same prefix as listings keeps its bare root.

Hosts mount several libraries under one ``<prefix>/api/`` (a monolith with a
service prefix). Anything this module registers at the bare ``v1/`` path
captures a sibling's list/create route at that address.
"""
import sys
import types

from django.http import JsonResponse
from django.test import SimpleTestCase, override_settings
from django.urls import include, path, resolve


def _sibling_root(request):
    return JsonResponse({"sibling": True})


_URLCONF = "_stapel_listings_shared_prefix_test_urls"
_mod = types.ModuleType(_URLCONF)
_mod.urlpatterns = [
    path("svc/api/", include("stapel_listings.urls")),
    path("svc/api/v1/", _sibling_root, name="sibling-root"),
]
sys.modules[_URLCONF] = _mod


@override_settings(ROOT_URLCONF=_URLCONF)
class SharedPrefixMountTests(SimpleTestCase):
    def test_sibling_root_resolves_past_listings(self):
        match = resolve("/svc/api/v1/")
        self.assertIs(match.func, _sibling_root)

    def test_own_routes_still_resolve_under_the_shared_prefix(self):
        self.assertEqual(resolve("/svc/api/v1/listings/").url_name, "listing-list")
