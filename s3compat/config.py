"""Endpoint configuration.

Credentials are only ever read from environment variables. A TOML file can add
or override endpoints, but it names the env vars to read rather than holding
secrets itself.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


@dataclass(frozen=True)
class EndpointSpec:
    """How to find an endpoint's settings in the environment."""

    name: str
    access_key_env: str
    secret_key_env: str
    endpoint_url_env: str | None = None
    default_endpoint_url: str | None = None
    region_env: str | None = None
    default_region: str = "us-east-1"
    session_token_env: str | None = None
    addressing_style: str = "auto"
    default_access_key: str | None = None
    default_secret_key: str | None = None


@dataclass
class Endpoint:
    name: str
    region: str
    access_key: str | None
    secret_key: str | None
    endpoint_url: str | None = None
    session_token: str | None = None
    addressing_style: str = "auto"
    verify_tls: bool = True
    missing: list[str] = field(default_factory=list)

    @property
    def has_credentials(self) -> bool:
        return bool(self.access_key and self.secret_key)

    def describe(self) -> str:
        url = self.endpoint_url or f"https://s3.{self.region}.amazonaws.com"
        return f"{self.name} ({url}, region={self.region})"


BUILTIN_SPECS: dict[str, EndpointSpec] = {
    "aws": EndpointSpec(
        name="aws",
        access_key_env="AWS_ACCESS_KEY_ID",
        secret_key_env="AWS_SECRET_ACCESS_KEY",
        session_token_env="AWS_SESSION_TOKEN",
        # Normally unset; pointing it at a moto server gives an offline stand-in.
        endpoint_url_env="AWS_ENDPOINT_URL_S3",
        region_env="AWS_REGION",
        default_region="us-east-1",
    ),
    "wasabi": EndpointSpec(
        name="wasabi",
        access_key_env="WASABI_ACCESS_KEY_ID",
        secret_key_env="WASABI_SECRET_ACCESS_KEY",
        endpoint_url_env="WASABI_ENDPOINT",
        default_endpoint_url="https://s3.wasabisys.com",
        region_env="WASABI_REGION",
        default_region="us-east-1",
    ),
    "minio": EndpointSpec(
        name="minio",
        access_key_env="MINIO_ACCESS_KEY_ID",
        secret_key_env="MINIO_SECRET_ACCESS_KEY",
        endpoint_url_env="MINIO_ENDPOINT",
        default_endpoint_url="http://localhost:9000",
        region_env="MINIO_REGION",
        default_region="us-east-1",
        addressing_style="path",
        # Stock credentials of the minio/minio image; only useful locally.
        default_access_key="minioadmin",
        default_secret_key="minioadmin",
    ),
}


def resolve(spec: EndpointSpec, env: Mapping[str, str] | None = None) -> Endpoint:
    env = os.environ if env is None else env

    def get(var: str | None, default: str | None = None) -> str | None:
        if not var:
            return default
        value = env.get(var, "").strip()
        return value or default

    access = get(spec.access_key_env, spec.default_access_key)
    secret = get(spec.secret_key_env, spec.default_secret_key)
    missing = [
        var
        for var, value in ((spec.access_key_env, access), (spec.secret_key_env, secret))
        if not value
    ]
    region = get(spec.region_env, spec.default_region) or "us-east-1"
    url = get(spec.endpoint_url_env, spec.default_endpoint_url)

    return Endpoint(
        name=spec.name,
        region=region,
        access_key=access,
        secret_key=secret,
        endpoint_url=url,
        session_token=get(spec.session_token_env),
        addressing_style=spec.addressing_style,
        missing=missing,
    )


def load_specs(config_path: str | Path | None = None) -> dict[str, EndpointSpec]:
    """Built-in specs, optionally extended or overridden by a TOML file.

    Example::

        [endpoints.ceph]
        endpoint_url = "https://ceph.internal:7480"
        region = "default"
        access_key_env = "CEPH_ACCESS_KEY_ID"
        secret_key_env = "CEPH_SECRET_ACCESS_KEY"
        addressing_style = "path"
    """
    specs = dict(BUILTIN_SPECS)
    if config_path is None:
        return specs

    with open(config_path, "rb") as fh:
        data = tomllib.load(fh)

    for name, raw in data.get("endpoints", {}).items():
        base = specs.get(name)
        upper = name.upper().replace("-", "_")
        access_env = raw.get("access_key_env") or (base.access_key_env if base else f"{upper}_ACCESS_KEY_ID")
        secret_env = raw.get("secret_key_env") or (base.secret_key_env if base else f"{upper}_SECRET_ACCESS_KEY")
        specs[name] = EndpointSpec(
            name=name,
            access_key_env=access_env,
            secret_key_env=secret_env,
            endpoint_url_env=raw.get("endpoint_url_env") or (base.endpoint_url_env if base else f"{upper}_ENDPOINT"),
            default_endpoint_url=raw.get("endpoint_url") or (base.default_endpoint_url if base else None),
            region_env=raw.get("region_env") or (base.region_env if base else f"{upper}_REGION"),
            default_region=raw.get("region") or (base.default_region if base else "us-east-1"),
            session_token_env=raw.get("session_token_env") or (base.session_token_env if base else None),
            addressing_style=raw.get("addressing_style") or (base.addressing_style if base else "auto"),
            default_access_key=base.default_access_key if base else None,
            default_secret_key=base.default_secret_key if base else None,
        )
    return specs


def load_endpoints(
    names: list[str],
    config_path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> list[Endpoint]:
    specs = load_specs(config_path)
    unknown = [n for n in names if n not in specs]
    if unknown:
        raise KeyError(f"unknown endpoint(s): {', '.join(unknown)}; known: {', '.join(specs)}")
    return [resolve(specs[n], env) for n in names]
