# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Snytro — https://snytro.com
"""A reviewed, portable source allowlist and retrieval policy."""
import json
from pathlib import Path, PurePosixPath

from .settings import atomic_json


def checked_profile(value):
    if not isinstance(value, dict) or set(value) - {"sources", "routes", "web_verification", "official_domains", "verify_original"}:
        raise ValueError("Profile supports sources, routes, web_verification, official_domains, and verify_original.")
    sources = value.get("sources")
    if not isinstance(sources, dict) or not sources:
        raise ValueError("Profile needs a nonempty sources object keyed by relative paths.")
    for path, policy in sources.items():
        name = PurePosixPath(path)
        if not name.parts or name.is_absolute() or ".." in name.parts or "\\" in path or name.as_posix() != path:
            raise ValueError("Profile paths must be normalized relative paths inside the source root.")
        if (not isinstance(policy, dict) or set(policy) - {"rank", "visibility"}
                or type(policy.get("rank")) is not int or not 1 <= policy["rank"] <= 20
                or policy.get("visibility") not in {"default", "explicit_only", "excluded"}):
            raise ValueError("Each source needs rank (1–20) and visibility (default, explicit_only, or excluded).")
    routes = value.get("routes", {})
    if not isinstance(routes, dict) or any(not name or not isinstance(paths, list) or not paths
                                           or any(not isinstance(path, str) or path not in sources for path in paths)
                                           for name, paths in routes.items()):
        raise ValueError("Each topic route must list paths present in sources.")
    web = value.get("web_verification", "off")
    domains = value.get("official_domains", [])
    if (web not in ("off", "when-needed", "required") or not isinstance(domains, list)
            or any(not isinstance(domain, str) or not domain or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-." for c in domain) for domain in domains)):
        raise ValueError("Set web_verification to off, when-needed, or required, and provide hostname-only official_domains.")
    verify = value.get("verify_original", True)
    if type(verify) is not bool:
        raise ValueError("verify_original must be a boolean.")
    return {**value, "routes": routes, "web_verification": web, "official_domains": domains, "verify_original": verify}


def profile_path(root, collection):
    return root / "profiles" / (collection + ".json")


def install_profile(root, collection, value, source_root):
    value = checked_profile(value)
    directory = Path(source_root).expanduser().resolve()
    if not directory.is_dir():
        raise ValueError("The profile source root must be an existing directory.")
    record = {"root": str(directory), "policy": value}
    atomic_json(profile_path(root, collection), record)
    return record


def load_profile(root, collection):
    path = profile_path(root, collection)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def source_policy(profile, path):
    if profile is None:
        return {"rank": 1, "visibility": "default"}
    try:
        relative = Path(path).relative_to(profile["root"]).as_posix()
    except ValueError:
        return None
    return profile["policy"]["sources"].get(relative)


def retrieval_scope(store, collection, profile, ids, include_explicit=False, route=None):
    if profile is None:
        if route:
            raise ValueError("No collection profile contains this route.")
        return ids, {}
    route_paths = None
    if route:
        route_paths = profile["policy"]["routes"].get(route)
        if not route_paths:
            raise ValueError("Route is not in the collection profile.")
        route_paths = {str(Path(profile["root"]) / path) for path in route_paths}
    allowed = {}
    for row in store.catalog_paths(collection, ids):
        if route_paths is not None and row["path"] not in route_paths:
            continue
        policy = source_policy(profile, row["path"])
        if policy and (policy["visibility"] == "default" or include_explicit and policy["visibility"] == "explicit_only"):
            allowed[row["id"]] = policy["rank"]
    return list(allowed), allowed
