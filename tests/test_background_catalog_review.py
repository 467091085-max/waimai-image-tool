from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import object_storage_service
from scripts import build_background_catalog as builder
from scripts import review_background_catalog as review

from tests.test_background_catalog_builder import save_test_image


def pending_category(
    tmp_path: Path,
    storage: object_storage_service.ObjectStorageService,
) -> dict[str, str]:
    entries = []
    hashes = {}
    for style_id in builder.background_catalog.STYLE_IDS:
        target = tmp_path / "light_food" / f"{style_id}.jpg"
        save_test_image(target)
        fingerprint = builder.app_module.image_file_fingerprint(target)
        prompt = builder.background_profiles.pure_background_prompt(
            "light_food",
            style_id,
        )
        prompt_sha = builder.hashlib.sha256(
            prompt.encode("utf-8")
        ).hexdigest()
        entry = {
            "schemaVersion": builder.background_catalog.CATALOG_SCHEMA_VERSION,
            "catalogVersion": builder.background_catalog.CATALOG_VERSION,
            "taxonomyVersion": builder.app_module.TAXONOMY_VERSION,
            "categoryId": "light_food",
            "categoryName": "轻食/沙拉",
            "styleId": style_id,
            "styleSlotId": builder.background_catalog.style_slot(style_id).slot_id,
            "styleSlotName": builder.background_catalog.style_slot(style_id).name,
            "styleSceneType": builder.background_catalog.style_slot(style_id).scene_type,
            "promptVersion": builder.PROMPT_VERSION,
            "promptSha256": prompt_sha,
            "provider": "tencent-hunyuan",
            "model": "hy-image-v3.0",
            "objectKey": builder.background_catalog.catalog_object_key(
                category_id="light_food",
                style_id=style_id,
                prompt_version=builder.PROMPT_VERSION,
                prompt_sha256=prompt_sha,
                asset_sha256=fingerprint["sha256"],
            ),
            "sha256": fingerprint["sha256"],
            "fileSize": fingerprint["fileSize"],
            "width": fingerprint["width"],
            "height": fingerprint["height"],
            "reviewStatus": "pending",
            "createdAt": "2026-08-01T00:00:00Z",
        }
        storage.put_bytes(
            target.read_bytes(),
            object_key=entry["objectKey"],
        )
        entries.append(entry)
        hashes[style_id] = fingerprint["sha256"]
    builder.upload_category_manifest(
        entries,
        category_id="light_food",
    )
    return hashes


def test_remote_manifest_resume_verifies_all_six_objects(tmp_path: Path) -> None:
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")
    with mock.patch.object(
        builder.object_storage_service,
        "get_object_storage_service",
        return_value=storage,
    ):
        hashes = pending_category(tmp_path, storage)
        entries = builder.reusable_remote_category_entries("light_food")

    assert entries is not None
    assert len(entries) == 6
    assert {entry["styleId"]: entry["sha256"] for entry in entries} == hashes
    assert all(entry["remoteManifestReused"] for entry in entries)


def test_remote_manifest_resume_rejects_changed_object(tmp_path: Path) -> None:
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")
    with mock.patch.object(
        builder.object_storage_service,
        "get_object_storage_service",
        return_value=storage,
    ):
        pending_category(tmp_path, storage)
        document = builder.remote_category_manifest_document("light_food")
        assert document is not None
        changed_key = document["assets"][0]["objectKey"]
        storage.put_bytes(b"changed", object_key=changed_key)
        entries = builder.reusable_remote_category_entries("light_food")

    assert entries is None


def test_execute_reuses_complete_remote_manifest_without_provider_cost(
    tmp_path: Path,
) -> None:
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")
    output = tmp_path / "work"
    with mock.patch.object(
        builder.object_storage_service,
        "get_object_storage_service",
        return_value=storage,
    ):
        pending_category(tmp_path, storage)
        with (
            mock.patch.object(builder.app_module, "tencent_ready", return_value=True),
            mock.patch.object(builder, "generate_entry") as provider,
        ):
            result = builder.main(
                [
                    "--category",
                    "light_food",
                    "--output",
                    str(output),
                    "--execute",
                    "--upload-pending",
                ]
            )

    assert result == 0
    provider.assert_not_called()
    report = json.loads((output / "run-report.json").read_text("utf-8"))
    assert report["complete"] is True
    assert report["remoteReusedAssetCount"] == 6
    assert len(report["manifestKeys"]) == 1


def test_execute_uploads_category_checkpoint_and_review_sheet(
    tmp_path: Path,
) -> None:
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")
    output = tmp_path / "generated"
    with (
        mock.patch.object(
            builder.object_storage_service,
            "get_object_storage_service",
            return_value=storage,
        ),
        mock.patch.object(builder.app_module, "tencent_ready", return_value=True),
        mock.patch.object(
            builder.app_module,
            "tencent_api_request",
            return_value={
                "ResultImage": "test-result",
                "_Provider": "tencent-hunyuan",
                "_Action": "TokenHubImageV3",
                "_Model": "hy-image-v3.0",
                "RequestId": "request-checkpoint",
            },
        ),
        mock.patch.object(
            builder.app_module,
            "save_result_image",
            side_effect=lambda _value, path: save_test_image(path),
        ),
        mock.patch.object(
            builder.app_module,
            "require_generated_output_quality",
            return_value={"status": "passed", "quality_score": 1.0},
        ),
    ):
        assert builder.main(
            [
                "--category",
                "light_food",
                "--output",
                str(output),
                "--execute",
                "--upload-pending",
            ]
        ) == 0

    report = json.loads((output / "run-report.json").read_text("utf-8"))
    assert len(report["manifestKeys"]) == 1
    document = json.loads(
        storage.read_bytes(report["manifestKeys"][0]).decode("utf-8")
    )
    assert document["reviewStatus"] == "pending"
    assert len(document["assets"]) == 6
    assert storage.exists(document["reviewSheet"]["objectKey"])


def test_review_approval_is_hash_locked_and_read_back_verified(
    tmp_path: Path,
) -> None:
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")
    storage_patch = mock.patch.object(
        builder.object_storage_service,
        "get_object_storage_service",
        return_value=storage,
    )
    review_storage_patch = mock.patch.object(
        review.object_storage_service,
        "get_object_storage_service",
        return_value=storage,
    )
    with storage_patch, review_storage_patch:
        hashes = pending_category(tmp_path, storage)
        result = review.approve_category_manifest(
            "light_food",
            expected_sha256=hashes,
            reviewer="catalog-reviewer",
            note="six exact previews passed",
        )

    assert result["reviewStatus"] == "approved"
    document = json.loads(
        storage.read_bytes(result["manifestKey"]).decode("utf-8")
    )
    assert document["reviewStatus"] == "approved"
    assert {asset["reviewStatus"] for asset in document["assets"]} == {
        "approved"
    }
    assert document["reviewedBy"] == "catalog-reviewer"
    assert document["reviewSheet"]["objectKey"] == result["reviewSheetKey"]
    assert storage.exists(result["reviewSheetKey"])


def test_review_approval_rejects_any_hash_mismatch(tmp_path: Path) -> None:
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")
    with (
        mock.patch.object(
            builder.object_storage_service,
            "get_object_storage_service",
            return_value=storage,
        ),
        mock.patch.object(
            review.object_storage_service,
            "get_object_storage_service",
            return_value=storage,
        ),
    ):
        hashes = pending_category(tmp_path, storage)
        hashes["style-6"] = "0" * 64
        try:
            review.approve_category_manifest(
                "light_food",
                expected_sha256=hashes,
                reviewer="catalog-reviewer",
                note="must fail",
            )
        except RuntimeError as exc:
            assert "does not match" in str(exc)
        else:
            raise AssertionError("hash mismatch must fail approval")


def test_expected_hash_parser_requires_all_six_slots() -> None:
    try:
        review.expected_hashes(["style-1=" + ("a" * 64)])
    except ValueError as exc:
        assert "exactly six" in str(exc)
    else:
        raise AssertionError("partial approval hashes must fail")
