from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import zipfile

import pytest
from PIL import Image

import app as app_module
import object_storage_service
from shared.batch_contract import freeze_menu_batch_contract


def _contract(*, image_count: int = 3, quality: str = "standard") -> dict:
    return freeze_menu_batch_contract(
        job_id="generation-batch-test",
        user_id="server-user",
        menu_upload_id="menu_" + ("a" * 32),
        menu={
            "objectKey": "menus/menu.xlsx",
            "sha256": "1" * 64,
            "parserVersion": 1,
        },
        selected_background={
            "assetId": "bg_test",
            "styleId": "style-1",
            "sha256": "2" * 64,
            "objectKey": "generated/selected-backgrounds/bg_test/image",
            "width": 1024,
            "height": 768,
        },
        generation_provenance=(
            app_module.generation_provenance_snapshot()
        ),
        quality=quality,
        image_count=image_count,
        platforms=["meituan"],
        watermark={"enabled": False},
        idempotency_key="browser-key",
        created_at="2026-07-29T12:00:00Z",
    )


def _selected_background(tmp_path: Path) -> app_module.SelectedBackgroundAsset:
    return app_module.SelectedBackgroundAsset(
        asset_id="bg_test",
        menu_key="1" * 12,
        style_id="style-1",
        sha256="2" * 64,
        path=tmp_path / "background.image",
        width=1024,
        height=768,
    )


def test_worker_partial_failure_refunds_only_undelivered_image_points(
    tmp_path: Path,
) -> None:
    contract = _contract(image_count=3, quality="premium")
    refunds: list[tuple[int, str]] = []
    updates: list[dict] = []
    plan = {"results": [], "styles": [], "summary": {"total": 3}}
    generation = {"status": "partial", "succeeded": 2, "failed": 1, "pending": 0}

    with (
        mock.patch.object(app_module, "materialize_menu_upload_snapshot", return_value=tmp_path / "menu.xlsx"),
        mock.patch.object(
            app_module,
            "parse_menu",
            return_value={"count": 3, "items": [], "store": "测试门店"},
        ),
        mock.patch.object(
            app_module,
            "selected_background_from_batch_contract",
            return_value=_selected_background(tmp_path),
        ),
        mock.patch.object(app_module, "build_plan", return_value=plan),
        mock.patch.object(app_module, "materialize_final_images", return_value=generation),
        mock.patch.object(
            app_module,
            "persist_generation_result_manifest",
            return_value={
                "objectKey": "generated/manifests/test/result.json",
                "sha256": "3" * 64,
                "size": 1,
                "requestSha256": contract["idempotency"]["requestSha256"],
            },
        ),
        mock.patch.object(
            app_module,
            "refund_generation_batch",
            side_effect=lambda _contract, *, points, reason: refunds.append((points, reason)),
        ),
        mock.patch.object(
            app_module,
            "update_persisted_generation_job",
            side_effect=lambda _job_id, **kwargs: updates.append(kwargs),
        ),
        mock.patch.object(app_module, "account_payload", return_value={"balance": 980}),
    ):
        result = app_module.run_generation_batch_job(contract)

    assert refunds == [(20, "partial_or_missing_outputs")]
    assert result["generationBatch"]["chargedPoints"] == 60
    assert result["generationBatch"]["refundedPoints"] == 20
    assert result["generationBatch"]["netPoints"] == 40
    assert updates[0]["status"] == "running"
    assert updates[-1]["status"] == "succeeded"
    assert updates[-1]["completed_count"] == 2
    assert updates[-1]["failed_count"] == 1


def test_worker_exception_refunds_full_server_charge_and_marks_failed(
    tmp_path: Path,
) -> None:
    contract = _contract(image_count=3)
    refunds: list[tuple[int, str]] = []
    updates: list[dict] = []

    with (
        mock.patch.object(app_module, "materialize_menu_upload_snapshot", return_value=tmp_path / "menu.xlsx"),
        mock.patch.object(app_module, "parse_menu", side_effect=RuntimeError("parser failed")),
        mock.patch.object(
            app_module,
            "refund_generation_batch",
            side_effect=lambda _contract, *, points, reason: refunds.append((points, reason)),
        ),
        mock.patch.object(
            app_module,
            "update_persisted_generation_job",
            side_effect=lambda _job_id, **kwargs: updates.append(kwargs),
        ),
    ):
        try:
            app_module.run_generation_batch_job(contract)
        except RuntimeError as exc:
            assert str(exc) == "parser failed"
        else:
            raise AssertionError("worker exception must fail the task")

    assert refunds == [(30, "RuntimeError")]
    assert updates[0]["status"] == "running"
    assert updates[-1]["status"] == "failed"
    assert updates[-1]["failed_count"] == 3


def test_worker_rejects_generation_provenance_drift_before_io_or_provider(
    tmp_path: Path,
) -> None:
    del tmp_path
    contract = _contract(image_count=3)
    contract["generationProvenance"]["modelVersion"] = "changed-model"
    materialize_menu = mock.Mock(
        side_effect=AssertionError("menu must not be read")
    )
    paid_provider = mock.Mock(
        side_effect=AssertionError("provider must not be called")
    )

    with (
        mock.patch.object(
            app_module,
            "materialize_menu_upload_snapshot",
            materialize_menu,
        ),
        mock.patch.object(
            app_module,
            "tencent_api_request",
            paid_provider,
        ),
        pytest.raises(app_module.MenuUploadError) as raised,
    ):
        app_module.execute_generation_batch_job(contract)

    assert raised.value.code == "generation_provenance_changed"
    materialize_menu.assert_not_called()
    paid_provider.assert_not_called()


def test_enqueue_rejection_compensates_the_exact_server_charge(
    tmp_path: Path,
) -> None:
    menu_upload_id = "menu_" + ("a" * 32)
    selected_background = _selected_background(tmp_path)
    menu_snapshot = {
        "id": menu_upload_id,
        "objectKey": "menus/menu.xlsx",
        "sha256": "1" * 64,
        "parserVersion": 1,
        "ownerUserId": "server-user",
        "originalFilename": "menu.xlsx",
        "summary": {"count": 3},
    }
    background_snapshot = {
        "assetId": "bg_test",
        "styleId": "style-1",
        "sha256": "2" * 64,
        "objectKey": "generated/selected-backgrounds/bg_test/image",
        "width": 1024,
        "height": 768,
    }
    failing_queue = SimpleNamespace(
        limits=SimpleNamespace(),
        fail_timed_out=lambda **_kwargs: [],
        get=lambda _job_id: None,
        enqueue=mock.Mock(side_effect=RuntimeError("generation queue admission denied: full")),
    )
    refunds: list[int] = []
    persisted_updates: list[dict] = []

    with (
        mock.patch.object(app_module, "generation_queue", failing_queue),
        mock.patch.object(
            app_module,
            "generation_request_principal",
            return_value=(
                {"userId": "server-user", "internal": False, "localDemo": False},
                None,
            ),
        ),
        mock.patch.object(app_module, "public_style_ids", return_value={"style-1"}),
        mock.patch.object(app_module, "tencent_ready", return_value=True),
        mock.patch.object(app_module, "resolve_menu_upload_snapshot", return_value=menu_snapshot),
        mock.patch.object(app_module, "materialize_menu_upload_snapshot", return_value=tmp_path / "menu.xlsx"),
        mock.patch.object(app_module, "requested_selected_background", return_value=selected_background),
        mock.patch.object(app_module, "selected_background_batch_snapshot", return_value=background_snapshot),
        mock.patch.object(app_module, "batch_watermark_snapshot", return_value={"enabled": False}),
        mock.patch.object(
            app_module.billing,
            "debit_account",
            return_value={"idempotent": False, "balance": 970},
        ),
        mock.patch.object(
            app_module,
            "persist_generation_batch_contract",
            return_value=({"status": "queued"}, True),
        ),
        mock.patch.object(
            app_module,
            "refund_generation_batch",
            side_effect=lambda _contract, *, points, reason: refunds.append(points),
        ),
        mock.patch.object(
            app_module,
            "update_persisted_generation_job",
            side_effect=lambda _job_id, **kwargs: persisted_updates.append(kwargs),
        ),
    ):
        response = app_module.app.test_client().post(
            "/api/generation-jobs",
            json={
                "style": "style-1",
                "quality": "standard",
                "jobId": "browser-key",
                "menuUploadId": menu_upload_id,
                "platforms": ["meituan"],
                "watermark": {"enabled": False},
                "imageCount": 9999,
                "points": 1,
                "userId": "attacker",
            },
        )

    assert response.status_code == 429
    assert response.get_json()["code"] == "generation_queue_full"
    assert refunds == [30]
    assert persisted_updates[-1]["status"] == "failed"
    assert persisted_updates[-1]["failed_count"] == 3


def test_result_manifest_is_first_writer_stable_and_request_bound(
    tmp_path: Path,
) -> None:
    contract = _contract()
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")
    first_result = {
        "generationBatch": app_module.public_generation_batch_payload(contract),
        "generation": {"succeeded": 3, "failed": 0},
        "results": [{"name": "first"}],
    }
    conflicting_retry = {
        **first_result,
        "results": [{"name": "must-not-overwrite"}],
    }

    with mock.patch.object(
        app_module.object_storage_service,
        "get_object_storage_service",
        return_value=storage,
    ):
        first = app_module.persist_generation_result_manifest(contract, first_result)
        second = app_module.persist_generation_result_manifest(
            contract,
            conflicting_retry,
        )
        loaded = app_module.load_generation_result_manifest_pointer(
            second,
            contract,
        )

    assert first == second
    assert loaded["results"][0]["name"] == "first"
    assert first["objectKey"].endswith(
        f"/{contract['idempotency']['requestSha256']}.json"
    )


def test_result_manifest_rejects_oversized_input_before_serialization(
    tmp_path: Path,
) -> None:
    contract = _contract()
    storage = object_storage_service.ObjectStorageService(
        tmp_path / "objects"
    )
    result = {
        "generationBatch": app_module.public_generation_batch_payload(
            contract
        ),
        "metadata": "x" * 512,
    }

    with (
        mock.patch.object(
            app_module.object_storage_service,
            "get_object_storage_service",
            return_value=storage,
        ),
        mock.patch.object(
            app_module,
            "MAX_GENERATION_MANIFEST_BYTES",
            128,
        ),
        mock.patch.object(
            app_module.json,
            "dumps",
            side_effect=AssertionError(
                "oversized manifest must not be serialized"
            ),
        ) as json_dumps,
    ):
        with pytest.raises(
            RuntimeError,
            match="manifest exceeds size limit",
        ):
            app_module.persist_generation_result_manifest(
                contract,
                result,
            )

    json_dumps.assert_not_called()
    assert storage.list_prefix("generated/manifests/") == []


def test_delivery_assets_are_shared_private_and_request_bound(
    tmp_path: Path,
) -> None:
    contract = _contract(image_count=1)
    source = tmp_path / "worker-output.png"
    Image.new("RGB", (16, 12), color=(220, 45, 35)).save(source, "PNG")
    plan = {
        "generationBatch": app_module.public_generation_batch_payload(contract),
        "generation": {"succeeded": 1, "failed": 0},
        "results": [
            {
                "row": 7,
                "name": "番茄炒蛋",
                "candidates": [
                    {
                        "imageId": "generated-row-7",
                        "path": str(source),
                        "url": "/media/worker-only.png",
                    }
                ],
            }
        ],
    }
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")

    with mock.patch.object(
        app_module.object_storage_service,
        "get_object_storage_service",
        return_value=storage,
    ):
        assets = app_module.persist_generation_delivery_assets(contract, plan)
        plan["deliveryAssets"] = assets
        manifest = app_module.persist_generation_result_manifest(contract, plan)
        stored_document = app_module.load_generation_result_manifest_document(
            manifest,
            contract,
        )
        public_result = app_module.load_generation_result_manifest_pointer(
            manifest,
            contract,
        )
        object_key, raw = app_module.generation_delivery_asset(
            stored_document,
            assets[0]["assetId"],
        )

    assert len(assets) == 1
    assert object_key.startswith("generated/delivery/generation-batch-test/")
    assert raw == source.read_bytes()
    assert plan["results"][0]["candidates"][0]["url"].endswith(
        f"/assets/{assets[0]['assetId']}"
    )
    assert "deliveryAssets" in stored_document
    assert "deliveryAssets" not in public_result
    assert "objectKey" not in str(public_result)
    assert "/media/worker-only.png" not in str(public_result)


def test_delivery_asset_rejects_oversized_object_before_body_read() -> None:
    asset_id = "asset_" + ("a" * 32)
    storage = SimpleNamespace(
        stat=mock.Mock(return_value={"size": 9}),
        read_bytes=mock.Mock(
            side_effect=AssertionError("oversized body must not be read")
        ),
    )
    result_document = {
        "deliveryAssets": [
            {
                "assetId": asset_id,
                "objectKey": "generated/delivery/job/image.png",
                "sha256": "b" * 64,
                "size": 9,
            }
        ]
    }

    with (
        mock.patch.object(app_module, "MAX_AI_ASSET_BYTES", 8),
        mock.patch.object(
            app_module.object_storage_service,
            "get_object_storage_service",
            return_value=storage,
        ),
        pytest.raises(app_module.MenuUploadError) as raised,
    ):
        app_module.generation_delivery_asset(result_document, asset_id)

    assert raised.value.code == "generation_asset_too_large"
    assert raised.value.status == 413
    storage.stat.assert_called_once_with(
        "generated/delivery/job/image.png"
    )
    storage.read_bytes.assert_not_called()


def test_export_uses_frozen_manifest_assets_instead_of_web_local_cache(
    tmp_path: Path,
) -> None:
    contract = _contract(image_count=1)
    source = tmp_path / "worker-output.png"
    Image.new("RGB", (640, 480), color=(210, 55, 40)).save(source, "PNG")
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")
    plan = {
        "generationBatch": app_module.public_generation_batch_payload(contract),
        "generation": {"succeeded": 1, "failed": 0},
        "results": [
            {
                "row": 5,
                "name": "番茄炒蛋",
                "category": "热销",
                "kind": "单品",
                "points": 10,
                "backgroundAction": "正式生成",
                "candidates": [
                    {
                        "imageId": "generated-row-5",
                        "path": str(source),
                        "url": "/media/worker-only.png",
                    }
                ],
            }
        ],
    }
    with mock.patch.object(
        app_module.object_storage_service,
        "get_object_storage_service",
        return_value=storage,
    ):
        plan["deliveryAssets"] = app_module.persist_generation_delivery_assets(
            contract,
            plan,
        )

    principal = {
        "userId": contract["userId"],
        "internal": False,
        "localDemo": False,
    }
    export_dir = tmp_path / "exports"
    with (
        mock.patch.object(
            app_module,
            "generation_request_principal",
            return_value=(principal, None),
        ),
        mock.patch.object(
            app_module,
            "load_persisted_generation_manifest_context",
            return_value=(plan, contract),
        ),
        mock.patch.object(
            app_module.object_storage_service,
            "get_object_storage_service",
            return_value=storage,
        ),
        mock.patch.object(app_module, "EXPORT_DIR", export_dir),
        mock.patch.object(
            app_module,
            "object_storage_export_payload",
            side_effect=lambda payload, **_kwargs: payload,
        ),
        mock.patch.object(
            app_module,
            "build_plan",
            side_effect=AssertionError("export must not rebuild the Web-local plan"),
        ),
    ):
        response = app_module.app.test_client().post(
            "/api/export",
            json={
                "jobId": contract["jobId"],
                "scope": "all",
                "platforms": ["meituan"],
                "watermark": {"enabled": True, "text": "tampered"},
            },
        )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["images"] == 1
    assert payload["platforms"] == ["meituan"]
    assert payload["watermark"] is False
    zip_path = export_dir / payload["download"].removeprefix("/download/")
    with zipfile.ZipFile(zip_path) as archive:
        image_names = [
            name for name in archive.namelist() if name.startswith("images/")
        ]
    assert len(image_names) == 1
    assert not list((export_dir / "_generation_sources").glob("*"))


def test_export_platforms_are_limited_to_the_frozen_purchase() -> None:
    contract = _contract(image_count=1)

    assert app_module.generation_contract_export_platforms(
        contract,
        ["meituan"],
    ) == ["meituan"]
    with pytest.raises(app_module.MenuUploadError) as raised:
        app_module.generation_contract_export_platforms(contract, ["jd"])

    assert raised.value.code == "generation_export_platform_not_authorized"


def test_result_manifest_rejects_wrong_request_pointer(tmp_path: Path) -> None:
    contract = _contract()
    storage = object_storage_service.ObjectStorageService(tmp_path / "objects")
    public_result = {
        "generationBatch": app_module.public_generation_batch_payload(contract),
        "generation": {"succeeded": 3, "failed": 0},
        "results": [],
    }

    with mock.patch.object(
        app_module.object_storage_service,
        "get_object_storage_service",
        return_value=storage,
    ):
        manifest = app_module.persist_generation_result_manifest(
            contract,
            public_result,
        )
        wrong = {**manifest, "requestSha256": "f" * 64}
        with pytest.raises(app_module.MenuUploadError) as raised:
            app_module.load_generation_result_manifest_pointer(wrong, contract)

    assert raised.value.code == "generation_manifest_request_mismatch"
