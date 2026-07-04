"""Django system checks for stapel-listings configuration.

Policy (docs/library-standard.md §3.7): E-level for configuration the
service cannot run with; W-level for entries that degrade lazily (a broken
*unused* dotted path must not block deploys).

Example:

    from django.core import checks

    @checks.register(checks.Tags.compatibility)
    def check_default_provider(app_configs, **kwargs):
        if ...:
            return [checks.Error("...", id="stapel_listings.E001")]
        return []
"""
