"""Canonical Pliqra configuration with a fallback for pre-rename deployments."""
import os


def env(name, default=None):
    if name in os.environ:
        return os.environ[name]
    legacy = name.replace('PLIQRA_', 'MODELPORT_', 1)
    return os.getenv(legacy, default)
