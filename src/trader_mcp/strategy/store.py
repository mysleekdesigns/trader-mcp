"""File-based, version-controllable strategy persistence (PRD §6 Phase 3).

One JSON file per strategy under ``{data_dir}/strategies/{slug}.json``, where the
slug is derived from the strategy ``name``. The JSON is the full
:class:`StrategySpec` (``model_dump_json``) plus a small ``_meta`` block carrying
``created``/``updated`` UTC timestamps and the ``schema_version`` -- so the file is
human-readable and diff-friendly (a strategy can live in a git repo).

Mirrors :class:`trader_mcp.data.store.OHLCVStore`'s construction pattern: cheap to
build, accepts an optional ``data_dir`` (defaults to ``settings.data_dir``), and
creates the directory lazily on first write.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from trader_mcp.config import ExchangeId, get_settings
from trader_mcp.errors import ValidationError
from trader_mcp.strategy.spec import StrategySpec

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class StrategyInfo(BaseModel):
    """A listing summary for one persisted strategy (no full spec load needed)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    exchange: ExchangeId
    symbol: str
    timeframe: str
    strategy_type: str
    schema_version: str
    created: datetime | None = None
    updated: datetime | None = None


def slugify(name: str) -> str:
    """Map a strategy ``name`` to a filesystem-safe slug (lowercase, hyphenated).

    Raises:
        trader_mcp.errors.ValidationError: if ``name`` has no slug-able characters.
    """
    slug = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    if not slug:
        raise ValidationError(
            f"Strategy name {name!r} produces an empty slug; use alphanumeric characters.",
            details={"kind": "invalid_strategy_name", "value": name},
        )
    return slug


class StrategyStore:
    """File-based store for declarative strategy specs (one JSON per strategy)."""

    def __init__(self, data_dir: Path | str | None = None) -> None:
        """Create a store rooted at ``{data_dir}/strategies`` (defaults from settings).

        Construction touches no filesystem; the directory is created on first save.
        """
        if data_dir is None:
            data_dir = get_settings().data_dir
        self.root: Path = Path(data_dir) / "strategies"

    def path_for(self, name: str) -> Path:
        """Return the JSON path for a strategy ``name`` (no I/O; may not exist)."""
        return self.root / f"{slugify(name)}.json"

    def exists(self, name: str) -> bool:
        """Return whether a saved strategy exists for ``name``."""
        return self.path_for(name).is_file()

    def save(self, spec: StrategySpec) -> StrategyInfo:
        """Persist ``spec`` as JSON, returning its listing info.

        On a first save, ``created`` and ``updated`` are stamped now (UTC). On an
        overwrite, ``created`` is preserved and ``updated`` is refreshed.
        """
        path = self.path_for(spec.name)
        now = datetime.now(tz=UTC)
        created = now
        if path.is_file():
            existing_meta = self._read_meta(path)
            created = existing_meta.get("created", now)
        path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "_meta": {
                "created": created.isoformat() if isinstance(created, datetime) else str(created),
                "updated": now.isoformat(),
                "schema_version": spec.schema_version,
            },
            "spec": json.loads(spec.model_dump_json()),
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(document, indent=2, sort_keys=False), encoding="utf-8")
        tmp.replace(path)
        return self._info(spec, created=created, updated=now)

    def load(self, name: str) -> StrategySpec:
        """Load and re-validate the strategy saved under ``name``.

        Raises:
            trader_mcp.errors.ValidationError: if no such strategy exists or the
                stored JSON no longer validates against the current schema.
        """
        path = self.path_for(name)
        if not path.is_file():
            raise ValidationError(
                f"No saved strategy named {name!r}.",
                details={"kind": "strategy_not_found", "name": name},
            )
        document = json.loads(path.read_text(encoding="utf-8"))
        spec_data = document.get("spec", document)
        return StrategySpec.model_validate(spec_data)

    def delete(self, name: str) -> bool:
        """Delete the strategy saved under ``name``; return whether it existed."""
        path = self.path_for(name)
        if not path.is_file():
            return False
        path.unlink()
        return True

    def list(self) -> list[StrategyInfo]:
        """Return summaries of every saved strategy, sorted by name."""
        if not self.root.is_dir():
            return []
        infos: list[StrategyInfo] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
                spec = StrategySpec.model_validate(document.get("spec", document))
            except (json.JSONDecodeError, OSError, ValueError):
                continue
            meta = self._parse_meta(document.get("_meta", {}))
            infos.append(self._info(spec, created=meta.get("created"), updated=meta.get("updated")))
        return sorted(infos, key=lambda i: i.name)

    # -- internals ---------------------------------------------------------------

    @staticmethod
    def _info(
        spec: StrategySpec, *, created: datetime | None, updated: datetime | None
    ) -> StrategyInfo:
        return StrategyInfo(
            name=spec.name,
            exchange=spec.exchange,
            symbol=spec.symbol,
            timeframe=spec.timeframe,
            strategy_type=spec.strategy_type,
            schema_version=spec.schema_version,
            created=created,
            updated=updated,
        )

    def _read_meta(self, path: Path) -> dict[str, Any]:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return self._parse_meta(document.get("_meta", {}))

    @staticmethod
    def _parse_meta(meta: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key in ("created", "updated"):
            raw = meta.get(key)
            if isinstance(raw, str):
                try:
                    out[key] = datetime.fromisoformat(raw).astimezone(UTC)
                except ValueError:
                    out[key] = None
        return out
