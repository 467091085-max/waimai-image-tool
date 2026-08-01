from __future__ import annotations

import hashlib

import background_catalog


def record(
    category_id: str,
    style_id: str,
    *,
    review_status: str = "approved",
) -> dict[str, str]:
    return {
        "catalogVersion": background_catalog.CATALOG_VERSION,
        "taxonomyVersion": background_catalog.TAXONOMY_VERSION,
        "categoryId": category_id,
        "styleId": style_id,
        "promptVersion": "style-background.v9",
        "reviewStatus": review_status,
        "sha256": hashlib.sha256(
            f"{category_id}/{style_id}".encode("utf-8")
        ).hexdigest(),
    }


def test_catalog_is_exactly_40_categories_by_six_slots() -> None:
    assert len(background_catalog.CATEGORY_IDS) == 40
    assert len(background_catalog.STYLE_SLOTS) == 6
    assert len(background_catalog.expected_catalog_pairs()) == 240
    assert len(set(background_catalog.expected_catalog_pairs())) == 240
    assert {
        slot.scene_type for slot in background_catalog.STYLE_SLOTS
    } == {"seamless-solid", "flat-table"}


def test_catalog_object_key_is_immutable_and_category_scoped() -> None:
    prompt_sha = "a" * 64
    asset_sha = "b" * 64

    key = background_catalog.catalog_object_key(
        category_id="light_food",
        style_id="style-3",
        prompt_version="style-background.v9",
        prompt_sha256=prompt_sha,
        asset_sha256=asset_sha,
    )

    assert key.startswith(
        "ai-assets/waimai-shared/background-catalog/background-catalog.v1/"
        "2026-07-30.v2/light_food/style-3/style-background.v9/"
    )
    assert "prompt-aaaaaaaaaaaaaaaa" in key
    assert key.endswith(f"/{asset_sha}.jpg")


def test_complete_manifest_requires_one_approved_asset_per_slot() -> None:
    records = [
        record("light_food", style_id)
        for style_id in background_catalog.STYLE_IDS
    ]

    status = background_catalog.category_manifest_status(
        records,
        category_id="light_food",
        prompt_version="style-background.v9",
    )

    assert status["ready"] is True
    assert status["approvedCount"] == 6
    assert status["missingStyleIds"] == []
    assert [asset["styleId"] for asset in status["assets"]] == list(
        background_catalog.STYLE_IDS
    )


def test_pending_missing_or_duplicate_approved_slots_fail_closed() -> None:
    records = [
        record(
            "light_food",
            style_id,
            review_status="pending" if style_id == "style-6" else "approved",
        )
        for style_id in background_catalog.STYLE_IDS
    ]
    records.append(record("light_food", "style-1"))

    status = background_catalog.category_manifest_status(
        records,
        category_id="light_food",
        prompt_version="style-background.v9",
    )

    assert status["ready"] is False
    assert status["missingStyleIds"] == ["style-6"]
    assert status["duplicateStyleIds"] == ["style-1"]
    assert status["pendingCount"] == 1


def test_mixed_is_not_a_catalog_category() -> None:
    try:
        background_catalog.normalize_category_id("mixed")
    except ValueError as exc:
        assert "unknown background category" in str(exc)
    else:
        raise AssertionError("mixed must not become a 41st catalog category")
