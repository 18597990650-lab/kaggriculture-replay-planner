"""Connect only the reconstructed Boey plan bank to the existing engine."""


def install(parent):
    import search_policy
    from boey02_library import install as install_library
    from service_restore import install as restore_service
    from production_completion import install as complete_production
    parent._RUNTIME.choose_programme = search_policy.choose_programme
    install_library(parent._RUNTIME)
    restore_service(parent._RUNTIME)
    complete_production(search_policy, parent._RUNTIME)
