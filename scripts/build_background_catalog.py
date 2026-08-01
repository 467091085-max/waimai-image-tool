from __future__ import annotations

import argparse
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any

from PIL import Image, ImageDraw, ImageOps


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app as app_module  # noqa: E402
import background_catalog  # noqa: E402
import background_profiles  # noqa: E402
import object_storage_service  # noqa: E402


PROMPT_VERSION = (
    f"style-background.v{app_module.STYLE_BACKGROUND_PROMPT_VERSION}"
)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def selected_categories(values: list[str]) -> tuple[str, ...]:
    if not values:
        return background_catalog.CATEGORY_IDS
    normalized = []
    for value in values:
        category_id = background_catalog.normalize_category_id(value)
        if category_id not in normalized:
            normalized.append(category_id)
    return tuple(normalized)


def selected_styles(values: list[str]) -> tuple[str, ...]:
    if not values:
        return background_catalog.STYLE_IDS
    normalized = []
    for value in values:
        style_id = background_catalog.style_slot(value).style_id
        if style_id not in normalized:
            normalized.append(style_id)
    return tuple(normalized)


def entry_paths(
    output: Path,
    category_id: str,
    style_id: str,
) -> tuple[Path, Path]:
    image = output / category_id / f"{style_id}.jpg"
    return image, image.with_suffix(".json")


def reusable_local_entry(
    image_path: Path,
    sidecar_path: Path,
    *,
    category_id: str,
    style_id: str,
    prompt_sha256: str,
) -> dict[str, Any] | None:
    if not image_path.is_file() or not sidecar_path.is_file():
        return None
    try:
        entry = json.loads(sidecar_path.read_text(encoding="utf-8"))
        fingerprint = app_module.image_file_fingerprint(image_path)
    except Exception:
        return None
    expected = {
        "catalogVersion": background_catalog.CATALOG_VERSION,
        "taxonomyVersion": app_module.TAXONOMY_VERSION,
        "categoryId": category_id,
        "styleId": style_id,
        "promptVersion": PROMPT_VERSION,
        "promptSha256": prompt_sha256,
        "sha256": fingerprint["sha256"],
    }
    if any(entry.get(key) != value for key, value in expected.items()):
        return None
    return entry


def remote_category_manifest_document(
    category_id: str,
) -> dict[str, Any] | None:
    category = background_catalog.normalize_category_id(category_id)
    key = background_catalog.catalog_manifest_key(
        category,
        PROMPT_VERSION,
        tenant_id=app_module.product_shared_asset_tenant_id(),
    )
    storage = object_storage_service.get_object_storage_service()
    raw = object_storage_service.read_object_bytes_limited_if_exists(
        storage,
        key,
        2 * 1024 * 1024,
    )
    if raw is None:
        return None
    document = json.loads(raw.decode("utf-8"))
    if not isinstance(document, dict):
        raise RuntimeError("background catalog manifest must be an object")
    return document


def reusable_remote_category_entries(
    category_id: str,
) -> list[dict[str, Any]] | None:
    category = background_catalog.normalize_category_id(category_id)
    document = remote_category_manifest_document(category)
    if document is None:
        return None
    expected_document = {
        "schemaVersion": background_catalog.CATALOG_SCHEMA_VERSION,
        "catalogVersion": background_catalog.CATALOG_VERSION,
        "taxonomyVersion": app_module.TAXONOMY_VERSION,
        "categoryId": category,
        "promptVersion": PROMPT_VERSION,
    }
    if any(
        document.get(key) != value
        for key, value in expected_document.items()
    ):
        return None
    review_status = str(document.get("reviewStatus") or "").lower()
    if review_status not in {"pending", "approved"}:
        return None
    raw_assets = document.get("assets")
    if not isinstance(raw_assets, list) or len(raw_assets) != 6:
        return None

    by_style: dict[str, dict[str, Any]] = {}
    for raw_asset in raw_assets:
        if not isinstance(raw_asset, dict):
            return None
        style_id = str(raw_asset.get("styleId") or "")
        if style_id not in background_catalog.STYLE_IDS or style_id in by_style:
            return None
        by_style[style_id] = raw_asset
    if set(by_style) != set(background_catalog.STYLE_IDS):
        return None

    storage = object_storage_service.get_object_storage_service()
    entries: list[dict[str, Any]] = []
    for style_id in background_catalog.STYLE_IDS:
        raw_asset = by_style[style_id]
        prompt = background_profiles.pure_background_prompt(
            category,
            style_id,
        )
        prompt_sha256 = hashlib.sha256(
            prompt.encode("utf-8")
        ).hexdigest()
        asset_sha256 = str(raw_asset.get("sha256") or "").lower()
        object_key = str(raw_asset.get("objectKey") or "")
        if not background_catalog.SHA256_RE.fullmatch(asset_sha256):
            return None
        try:
            file_size = int(raw_asset.get("fileSize") or 0)
            expected_key = background_catalog.catalog_object_key(
                category_id=category,
                style_id=style_id,
                prompt_version=PROMPT_VERSION,
                prompt_sha256=prompt_sha256,
                asset_sha256=asset_sha256,
                suffix=Path(object_key).suffix,
                tenant_id=app_module.product_shared_asset_tenant_id(),
            )
        except (TypeError, ValueError):
            return None
        expected_asset = {
            "catalogVersion": background_catalog.CATALOG_VERSION,
            "taxonomyVersion": app_module.TAXONOMY_VERSION,
            "categoryId": category,
            "styleId": style_id,
            "promptVersion": PROMPT_VERSION,
            "promptSha256": prompt_sha256,
            "objectKey": expected_key,
            "reviewStatus": review_status,
        }
        if any(
            raw_asset.get(key) != value
            for key, value in expected_asset.items()
        ):
            return None
        if not 0 < file_size <= app_module.MAX_AI_ASSET_BYTES:
            return None
        stored = object_storage_service.read_object_bytes_limited(
            storage,
            object_key,
            app_module.MAX_AI_ASSET_BYTES,
        )
        if len(stored) != file_size:
            return None
        if hashlib.sha256(stored).hexdigest() != asset_sha256:
            return None
        entries.append(
            {
                **raw_asset,
                "uploaded": True,
                "remoteManifestReused": True,
            }
        )
    return entries


def provider_model(response: dict[str, Any]) -> str:
    return str(response.get("_Model") or "hy-image-v3.0").strip()


def deterministic_generation_seed(
    category_id: str,
    style_id: str,
    attempt: int = 1,
) -> int:
    identity = "|".join(
        (
            background_catalog.CATALOG_VERSION,
            app_module.TAXONOMY_VERSION,
            PROMPT_VERSION,
            background_catalog.normalize_category_id(category_id),
            background_catalog.style_slot(style_id).style_id,
        )
    )
    if attempt > 1:
        identity += f"|retry-{attempt}"
    seed = int.from_bytes(
        hashlib.sha256(identity.encode("utf-8")).digest()[:4],
        "big",
    )
    return seed or 1


def generate_entry(
    *,
    category_id: str,
    style_id: str,
    image_path: Path,
    attempts: int,
    seed_revision: int = 0,
) -> dict[str, Any]:
    prompt = background_profiles.pure_background_prompt(
        category_id,
        style_id,
    )
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    accepted_seed = deterministic_generation_seed(
        category_id,
        style_id,
        seed_revision + 1,
    )
    image_path.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    started = time.time()
    response: dict[str, Any] = {}
    for attempt in range(1, attempts + 1):
        requested_seed = deterministic_generation_seed(
            category_id,
            style_id,
            seed_revision + attempt,
        )
        try:
            response = app_module.tencent_api_request(
                "TextToImageLite",
                {
                    "Prompt": prompt,
                    "NegativePrompt": (
                        background_profiles.PURE_BACKGROUND_NEGATIVE_PROMPT
                    ),
                    "Resolution": app_module.default_delivery_resolution(),
                    "RspImgType": "url",
                    "LogoAdd": 0,
                    "Revise": 0,
                    "Seed": requested_seed,
                },
            )
            app_module.save_result_image(
                str(response.get("ResultImage") or ""),
                image_path,
            )
            accepted_seed = requested_seed
            break
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                raise
            time.sleep(min(15, attempt * 3))
    if not response:
        raise RuntimeError(str(last_error or "background provider returned no result"))

    quality_report = app_module.require_generated_output_quality(image_path)
    fingerprint = app_module.image_file_fingerprint(image_path)
    slot = background_catalog.style_slot(style_id)
    object_key = background_catalog.catalog_object_key(
        category_id=category_id,
        style_id=style_id,
        prompt_version=PROMPT_VERSION,
        prompt_sha256=prompt_sha256,
        asset_sha256=str(fingerprint["sha256"]),
        tenant_id=app_module.product_shared_asset_tenant_id(),
        suffix=image_path.suffix,
    )
    return {
        "schemaVersion": background_catalog.CATALOG_SCHEMA_VERSION,
        "catalogVersion": background_catalog.CATALOG_VERSION,
        "taxonomyVersion": app_module.TAXONOMY_VERSION,
        "categoryId": category_id,
        "categoryName": background_catalog.category_label(category_id),
        "styleId": style_id,
        "styleSlotId": slot.slot_id,
        "styleSlotName": slot.name,
        "styleSceneType": slot.scene_type,
        "promptVersion": PROMPT_VERSION,
        "promptSha256": prompt_sha256,
        "provider": str(response.get("_Provider") or "tencent-hunyuan"),
        "providerAction": str(response.get("_Action") or "TextToImageLite"),
        "model": provider_model(response),
        "requestId": str(response.get("RequestId") or ""),
        "seed": response.get("Seed") or accepted_seed,
        "promptRevisionEnabled": False,
        "objectKey": object_key,
        "sha256": str(fingerprint["sha256"]),
        "width": int(fingerprint["width"]),
        "height": int(fingerprint["height"]),
        "fileSize": int(fingerprint["fileSize"]),
        "qualityReport": quality_report,
        "reviewStatus": "pending",
        "localPath": str(image_path.resolve()),
        "elapsedSeconds": round(time.time() - started, 2),
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def register_pending_entry(
    entry: dict[str, Any],
    image_path: Path,
    *,
    actor_user_id: str,
) -> dict[str, Any]:
    if not actor_user_id:
        raise ValueError(
            "--actor-user-id or BACKGROUND_CATALOG_ACTOR_USER_ID is required"
        )
    if not app_module.product_asset_library_store.IDENTIFIER_RE.fullmatch(
        actor_user_id
    ):
        raise ValueError("invalid background catalog actor user id")
    raw = image_path.read_bytes()
    object_key, digest, existed = upload_pending_entry(entry, image_path)

    tenant_id = app_module.product_shared_asset_tenant_id()
    identity = {
        "catalogVersion": entry["catalogVersion"],
        "taxonomyVersion": entry["taxonomyVersion"],
        "categoryId": entry["categoryId"],
        "styleId": entry["styleId"],
        "promptSha256": entry["promptSha256"],
        "sha256": entry["sha256"],
    }
    idempotency_key = "catalog-" + hashlib.sha256(
        json.dumps(identity, sort_keys=True).encode("utf-8")
    ).hexdigest()
    model = app_module.product_asset_version_token(
        entry.get("model"),
        "hy-image-v3.0",
    )
    try:
        with app_module.postgres_connection() as connection:
            result = app_module.product_asset_library_store.ProductAssetLibraryStore(
                connection
            ).register_asset(
                tenant_id=tenant_id,
                owner_user_id=actor_user_id,
                idempotency_key=idempotency_key,
                asset_kind="background",
                taxonomy_version=app_module.TAXONOMY_VERSION,
                category_id=str(entry["categoryId"]),
                category_name=str(entry["categoryName"]),
                style_id=str(entry["styleId"]),
                background_asset_id="",
                background_sha256="",
                standard_name=f"{entry['categoryName']}背景",
                aliases=[],
                match_keywords=[
                    str(entry["categoryName"]),
                    str(entry["styleSlotName"]),
                ],
                reuse_scope="tenant",
                source_kind="generated",
                source_provider=str(entry["provider"])[:128],
                prompt_version=PROMPT_VERSION,
                model_name=model,
                model_version=model,
                pipeline_version=app_module.product_asset_pipeline_version(
                    "category_background"
                ),
                original_object_ref=object_key,
                original_sha256=digest,
                original_size_bytes=len(raw),
            )
    except Exception:
        if not existed:
            storage.delete(object_key)
        raise
    return {
        **entry,
        "assetRecordId": str(result.record["id"]),
        "assetStatus": str(result.record["status"]),
        "reviewStatus": str(result.record["review_status"]),
        "registered": True,
        "registrationCreated": bool(result.created),
        "uploaded": True,
    }


def upload_pending_entry(
    entry: dict[str, Any],
    image_path: Path,
) -> tuple[str, str, bool]:
    raw = image_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != entry["sha256"]:
        raise RuntimeError("background changed before catalog upload")
    object_key = object_storage_service.validate_object_key(
        str(entry["objectKey"])
    )
    storage = object_storage_service.get_object_storage_service()
    existed = storage.exists(object_key)
    if existed:
        stored = object_storage_service.read_object_bytes_limited(
            storage,
            object_key,
            app_module.MAX_AI_ASSET_BYTES,
        )
        if (
            len(stored) != len(raw)
            or hashlib.sha256(stored).hexdigest() != digest
        ):
            raise RuntimeError("existing catalog object content changed")
        return object_key, digest, True

    stored_key = storage.put_bytes(raw, object_key=object_key)
    if stored_key != object_key:
        raise RuntimeError("background catalog object key mismatch")
    try:
        stored = object_storage_service.read_object_bytes_limited(
            storage,
            object_key,
            app_module.MAX_AI_ASSET_BYTES,
        )
        if (
            len(stored) != len(raw)
            or hashlib.sha256(stored).hexdigest() != digest
        ):
            raise RuntimeError(
                "background catalog read-back verification failed"
            )
    except Exception:
        storage.delete(object_key)
        raise
    return object_key, digest, False


def upload_category_manifest(
    entries: list[dict[str, Any]],
    *,
    category_id: str,
) -> str:
    if {entry["styleId"] for entry in entries} != set(
        background_catalog.STYLE_IDS
    ):
        raise RuntimeError("category manifest requires all six style slots")
    public_entries = [
        {
            key: value
            for key, value in entry.items()
            if key not in {"localPath", "remoteManifestReused"}
        }
        for entry in sorted(entries, key=lambda item: item["styleId"])
    ]
    review_sheet = upload_category_review_sheet(
        entries,
        category_id=category_id,
    )
    document = {
        "schemaVersion": background_catalog.CATALOG_SCHEMA_VERSION,
        "catalogVersion": background_catalog.CATALOG_VERSION,
        "taxonomyVersion": app_module.TAXONOMY_VERSION,
        "categoryId": category_id,
        "categoryName": background_catalog.category_label(category_id),
        "promptVersion": PROMPT_VERSION,
        "reviewStatus": "pending",
        "reviewSheet": review_sheet,
        "assets": public_entries,
        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    key = background_catalog.catalog_manifest_key(
        category_id,
        PROMPT_VERSION,
        tenant_id=app_module.product_shared_asset_tenant_id(),
    )
    payload = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    storage = object_storage_service.get_object_storage_service()
    returned = storage.put_bytes(payload, object_key=key)
    if returned != key:
        raise RuntimeError("category manifest object key mismatch")
    readback = object_storage_service.read_object_bytes_limited(
        storage,
        key,
        2 * 1024 * 1024,
    )
    if hashlib.sha256(readback).digest() != hashlib.sha256(payload).digest():
        raise RuntimeError("category manifest read-back verification failed")
    return key


def upload_category_review_sheet(
    entries: list[dict[str, Any]],
    *,
    category_id: str,
) -> dict[str, Any]:
    category = background_catalog.normalize_category_id(category_id)
    by_style = {
        str(entry.get("styleId") or ""): entry
        for entry in entries
    }
    if set(by_style) != set(background_catalog.STYLE_IDS):
        raise RuntimeError("review sheet requires all six style slots")
    storage = object_storage_service.get_object_storage_service()
    tile_width = 512
    image_height = 384
    label_height = 28
    canvas = Image.new(
        "RGB",
        (tile_width * 3, (image_height + label_height) * 2),
        (242, 242, 242),
    )
    draw = ImageDraw.Draw(canvas)
    for index, style_id in enumerate(background_catalog.STYLE_IDS):
        entry = by_style[style_id]
        raw = object_storage_service.read_object_bytes_limited(
            storage,
            str(entry["objectKey"]),
            app_module.MAX_AI_ASSET_BYTES,
        )
        if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise RuntimeError(f"review sheet source changed for {style_id}")
        with Image.open(BytesIO(raw)) as source:
            tile = ImageOps.fit(
                source.convert("RGB"),
                (tile_width, image_height),
                method=Image.Resampling.LANCZOS,
            )
        column = index % 3
        row = index // 3
        x = column * tile_width
        y = row * (image_height + label_height)
        canvas.paste(tile, (x, y))
        draw.rectangle(
            (x, y + image_height, x + tile_width, y + image_height + label_height),
            fill=(24, 24, 24),
        )
        slot = background_catalog.style_slot(style_id)
        draw.text(
            (x + 10, y + image_height + 7),
            f"{style_id}  {slot.slot_id}",
            fill=(255, 255, 255),
        )
    output = BytesIO()
    canvas.save(output, "JPEG", quality=92, optimize=True)
    payload = output.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    manifest_key = background_catalog.catalog_manifest_key(
        category,
        PROMPT_VERSION,
        tenant_id=app_module.product_shared_asset_tenant_id(),
    )
    object_key = (
        manifest_key.rsplit("/", 1)[0]
        + f"/review/contact-sheet-{digest}.jpg"
    )
    if storage.put_bytes(payload, object_key=object_key) != object_key:
        raise RuntimeError("review contact-sheet object key mismatch")
    readback = object_storage_service.read_object_bytes_limited(
        storage,
        object_key,
        4 * 1024 * 1024,
    )
    if hashlib.sha256(readback).hexdigest() != digest:
        raise RuntimeError("review contact-sheet read-back verification failed")
    return {
        "objectKey": object_key,
        "sha256": digest,
        "fileSize": len(payload),
        "width": canvas.width,
        "height": canvas.height,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate the reviewed 40-category x 6-slot background catalog. "
            "Without --execute this command only prints the exact plan."
        )
    )
    parser.add_argument("--category", action="append", default=[])
    parser.add_argument("--style", action="append", default=[])
    parser.add_argument(
        "--output",
        default=str(ROOT / "data" / "background_catalog_work" / PROMPT_VERSION),
    )
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument(
        "--regenerate-selected",
        action="store_true",
        help=(
            "Replace only the selected --style slots in one existing pending "
            "remote category manifest."
        ),
    )
    parser.add_argument(
        "--seed-revision",
        type=int,
        default=0,
        help=(
            "Deterministic seed offset for an explicitly regenerated slot; "
            "revision 1 starts from retry seed 2."
        ),
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--upload-pending", action="store_true")
    parser.add_argument("--register-pending", action="store_true")
    parser.add_argument(
        "--actor-user-id",
        default=os.environ.get("BACKGROUND_CATALOG_ACTOR_USER_ID", ""),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    categories = selected_categories(args.category)
    styles = selected_styles(args.style)
    if args.seed_revision < 0:
        raise SystemExit("--seed-revision must be zero or greater")
    if args.regenerate_selected:
        if len(categories) != 1 or not args.category:
            raise SystemExit(
                "--regenerate-selected requires exactly one --category"
            )
        if not args.style:
            raise SystemExit(
                "--regenerate-selected requires at least one --style"
            )
        if not args.upload_pending or args.register_pending:
            raise SystemExit(
                "--regenerate-selected requires --upload-pending and does not "
                "support --register-pending"
            )
        if args.seed_revision < 1:
            raise SystemExit(
                "--regenerate-selected requires --seed-revision of at least 1"
            )
    pairs = [
        (category_id, style_id)
        for category_id in categories
        for style_id in styles
    ]
    plan = {
        "catalogVersion": background_catalog.CATALOG_VERSION,
        "taxonomyVersion": app_module.TAXONOMY_VERSION,
        "promptVersion": PROMPT_VERSION,
        "categoryCount": len(categories),
        "styleCount": len(styles),
        "plannedAssetCount": len(pairs),
        "categories": list(categories),
        "styles": list(styles),
        "execute": bool(args.execute),
        "uploadPending": bool(args.upload_pending),
        "registerPending": bool(args.register_pending),
        "regenerateSelected": bool(args.regenerate_selected),
        "seedRevision": int(args.seed_revision),
    }
    if not args.execute:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    if args.register_pending and not args.actor_user_id:
        raise SystemExit(
            "--register-pending requires --actor-user-id or "
            "BACKGROUND_CATALOG_ACTOR_USER_ID"
        )
    if not app_module.tencent_ready():
        raise SystemExit("Tencent Hunyuan/TokenHub provider is not ready")

    output = Path(args.output).expanduser().resolve()
    report_path = output / "run-report.json"
    entries: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    manifest_keys: list[str] = []
    manifested_categories: set[str] = set()
    remote_entries: dict[tuple[str, str], dict[str, Any]] = {}
    if args.upload_pending and not args.register_pending:
        for category_id in categories:
            try:
                reusable = reusable_remote_category_entries(category_id)
            except Exception as exc:
                detail = re.sub(r"\s+", " ", str(exc))[:300]
                print(
                    f"REMOTE-MISS {category_id} "
                    f"{type(exc).__name__}: {detail}"
                )
                continue
            for entry in reusable or []:
                remote_entries[(category_id, entry["styleId"])] = entry
    if args.regenerate_selected:
        category_id = categories[0]
        document = remote_category_manifest_document(category_id)
        if document is None or len(
            [
                entry
                for (entry_category, _style_id), entry in remote_entries.items()
                if entry_category == category_id
            ]
        ) != len(background_catalog.STYLE_IDS):
            raise SystemExit(
                "--regenerate-selected requires a complete verified remote "
                "category manifest"
            )
        if str(document.get("reviewStatus") or "").lower() != "pending":
            raise SystemExit(
                "--regenerate-selected refuses to replace an approved manifest"
            )
    for category_id, style_id in pairs:
        image_path, sidecar_path = entry_paths(
            output,
            category_id,
            style_id,
        )
        prompt = background_profiles.pure_background_prompt(
            category_id,
            style_id,
        )
        prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        try:
            entry = None
            if not args.regenerate_selected:
                entry = remote_entries.get((category_id, style_id))
            if entry is None and not args.regenerate_selected:
                entry = reusable_local_entry(
                    image_path,
                    sidecar_path,
                    category_id=category_id,
                    style_id=style_id,
                    prompt_sha256=prompt_sha256,
                )
            if entry is None:
                entry = generate_entry(
                    category_id=category_id,
                    style_id=style_id,
                    image_path=image_path,
                    attempts=max(1, min(5, int(args.attempts))),
                    seed_revision=int(args.seed_revision),
                )
            if args.register_pending and not entry.get("registered"):
                entry = register_pending_entry(
                    entry,
                    image_path,
                    actor_user_id=args.actor_user_id,
                )
            elif args.upload_pending and not entry.get("uploaded"):
                upload_pending_entry(entry, image_path)
                entry = {**entry, "uploaded": True}
            write_json(sidecar_path, entry)
            entries.append(entry)
            processed_category_entries = [
                item
                for item in entries
                if item["categoryId"] == category_id
            ]
            category_entries = processed_category_entries
            if args.regenerate_selected:
                by_style = {
                    remote_style: remote_entry
                    for (
                        remote_category,
                        remote_style,
                    ), remote_entry in remote_entries.items()
                    if remote_category == category_id
                }
                by_style.update(
                    {
                        item["styleId"]: item
                        for item in processed_category_entries
                    }
                )
                category_entries = [
                    by_style[required_style]
                    for required_style in background_catalog.STYLE_IDS
                    if required_style in by_style
                ]
            if (
                (args.register_pending or args.upload_pending)
                and category_id not in manifested_categories
                and {
                    item["styleId"]
                    for item in processed_category_entries
                }
                == set(styles)
                and {item["styleId"] for item in category_entries}
                == set(background_catalog.STYLE_IDS)
            ):
                if all(
                    item.get("remoteManifestReused")
                    for item in category_entries
                ):
                    manifest_key = background_catalog.catalog_manifest_key(
                        category_id,
                        PROMPT_VERSION,
                        tenant_id=(
                            app_module.product_shared_asset_tenant_id()
                        ),
                    )
                else:
                    manifest_key = upload_category_manifest(
                        category_entries,
                        category_id=category_id,
                    )
                manifest_keys.append(manifest_key)
                manifested_categories.add(category_id)
            print(
                f"PASS {category_id}/{style_id} "
                f"sha256={entry['sha256']} review={entry['reviewStatus']}"
            )
        except Exception as exc:
            failure = {
                "categoryId": category_id,
                "styleId": style_id,
                "errorType": type(exc).__name__,
                "error": re.sub(r"\s+", " ", str(exc))[:500],
            }
            failures.append(failure)
            print(
                f"FAIL {category_id}/{style_id} "
                f"{failure['errorType']}: {failure['error']}"
            )
        write_json(
            report_path,
            {
                **plan,
                "completedAssetCount": len(entries),
                "failureCount": len(failures),
                "manifestKeys": manifest_keys,
                "entries": entries,
                "failures": failures,
            },
        )

    final_report = {
        **plan,
        "completedAssetCount": len(entries),
        "failureCount": len(failures),
        "complete": not failures and len(entries) == len(pairs),
        "remoteReusedAssetCount": sum(
            bool(entry.get("remoteManifestReused"))
            for entry in entries
        ),
        "manifestKeys": manifest_keys,
        "entries": entries,
        "failures": failures,
    }
    write_json(report_path, final_report)
    print(
        json.dumps(
            {
                "complete": final_report["complete"],
                "completedAssetCount": len(entries),
                "failureCount": len(failures),
                "report": str(report_path),
            },
            ensure_ascii=False,
        )
    )
    return 0 if final_report["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
