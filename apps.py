from django.apps import AppConfig


class ListingsConfig(AppConfig):
    name = "stapel_listings"
    label = "listings"
    verbose_name = "Listings and catalog"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Import-time side effects: comm functions/actions, system checks,
        # error-key registration, GDPR provider. Keep each in its own module.
        from . import checks  # noqa: F401
        from . import errors  # noqa: F401
        from . import functions  # noqa: F401

        # GDPR: listings hold user data, so a provider is required.
        from stapel_core.gdpr import gdpr_registry

        from .gdpr import ListingsGDPRProvider

        gdpr_registry.register(ListingsGDPRProvider())

        # Action subscriptions (category.changed, moderation.completed,
        # user.deleted) — in-process in a monolith, bus consumer in
        # microservices; same code, transport chosen by STAPEL_COMM.
        from . import actions  # noqa: F401
