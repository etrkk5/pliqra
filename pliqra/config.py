"""Canonical Pliqra configuration with a fallback for pre-rename deployments."""
import os
import ssl


def env(name, default=None):
    if name in os.environ:
        return os.environ[name]
    legacy = name.replace('PLIQRA_', 'MODELPORT_', 1)
    return os.getenv(legacy, default)


def http_verify():
    bundle=env('PLIQRA_CA_BUNDLE')
    return ssl.create_default_context(cafile=bundle) if bundle else True
