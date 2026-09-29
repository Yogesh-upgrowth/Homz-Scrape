"""Image ingestion: URL classification, content-addressed storage, watermark."""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image as PILImage

from homz.images.store import ImageStore
from homz.images.urls import ImageKind, classify, filter_photos, is_photo
from homz.images.watermark import remove_watermark


class TestClassify:
    @pytest.mark.parametrize("url", [
        "https://static.squareyards.com/ui-assets/images/google-play.png",
        "https://static.squareyards.com/ui-assets/images/app-store.png",
        "https://img.staticmb.com/mbimages/user/Photo_h180_w240/60106225_X_180_240.jpg",
        "https://img.staticmb.com/mbimages/topagent/Profile-1-3007728_266_200.jpeg",
    ])
    def test_known_junk_is_rejected(self, url: str) -> None:
        kind, reason = classify(url)
        assert kind is ImageKind.JUNK
        assert reason

    @pytest.mark.parametrize("url", [
        # Full-resolution owner uploads attached to a review. The path name
        # reads like avatars but these are real property photos - 46,562 of
        # them in the live corpus, and often a listing's only interior shots.
        "https://static.squareyards.com/reviewrating/images/1781238262320.jpg",
        "https://img.staticmb.com/mbphoto/property/cropped_images/ver2/A/Photo_h600_w900/8_1_x_600_900.jpg",
        "https://img.squareyards.com/secondaryPortal/optImages/IN_639138408991925578-0805.jpg",
        "https://static.squareyards.com/resources/images/gurgaon/project-image/x-image1.jpg",
    ])
    def test_real_photos_are_kept(self, url: str) -> None:
        assert is_photo(url)

    def test_empty_url_is_junk(self) -> None:
        assert classify("")[0] is ImageKind.JUNK
        assert classify("   ")[0] is ImageKind.JUNK

    def test_unknown_host_defaults_to_photo(self) -> None:
        # Allow-by-default: a missed junk URL costs one wasted download, a
        # wrongly-dropped one silently loses real content.
        assert is_photo("https://cdn.example.com/some/listing/photo.jpg")

    def test_filter_photos_preserves_order(self) -> None:
        urls = [
            "https://img.staticmb.com/mbphoto/property/cropped_images/a_600_900.jpg",
            "https://static.squareyards.com/ui-assets/images/google-play.png",
            "https://static.squareyards.com/reviewrating/images/1.jpg",
        ]
        assert filter_photos(urls) == [urls[0], urls[2]]


class TestImageStore:
    def _png(self, colour: tuple[int, int, int]) -> bytes:
        buf = io.BytesIO()
        PILImage.new("RGB", (8, 8), colour).save(buf, format="PNG")
        return buf.getvalue()

    def test_same_bytes_stored_once(self, tmp_path) -> None:
        store = ImageStore(tmp_path, enabled=True)
        payload = self._png((10, 20, 30))
        key1, digest1, new1, _ = store.put(payload)
        key2, digest2, new2, _ = store.put(payload)
        assert (key1, digest1) == (key2, digest2)
        assert new1 is True and new2 is False
        assert len(list(tmp_path.rglob("*.webp"))) == 1

    def test_different_bytes_get_different_keys(self, tmp_path) -> None:
        store = ImageStore(tmp_path, enabled=True)
        k1, *_ = store.put(self._png((1, 2, 3)))
        k2, *_ = store.put(self._png((9, 9, 9)))
        assert k1 != k2

    def test_roundtrip(self, tmp_path) -> None:
        store = ImageStore(tmp_path, enabled=True)
        payload = self._png((77, 88, 99))
        key, *_ = store.put(payload)
        assert store.exists(key)
        assert store.get(key) == payload

    def test_key_is_relative_and_fanned_out(self, tmp_path) -> None:
        # The stored key must stay backend-agnostic so the archive can move to
        # S3/R2 without touching the schema.
        store = ImageStore(tmp_path, enabled=True)
        key, digest, _, _ = store.put(self._png((5, 5, 5)))
        assert key == f"_pool/{digest[:2]}/{digest[2:4]}/{digest}.webp"
        assert not key.startswith("/")

    def test_get_missing_returns_none(self, tmp_path) -> None:
        assert ImageStore(tmp_path, enabled=True).get("de/ad/deadbeef.webp") is None

    def test_no_partial_files_left_behind(self, tmp_path) -> None:
        store = ImageStore(tmp_path, enabled=True)
        store.put(self._png((3, 3, 3)))
        assert list(tmp_path.rglob("*.part")) == []


class TestWatermark:
    def _img(self, w: int = 800, h: int = 600) -> np.ndarray:
        rng = np.random.default_rng(0)
        return rng.integers(0, 255, (h, w, 3), dtype=np.uint8)

    def test_unknown_source_is_untouched(self) -> None:
        img = self._img()
        result = remove_watermark(img, "housing")
        assert result.removed is None
        assert np.array_equal(result.image, img)

    @pytest.mark.parametrize(("source", "url"), [
        # Same dimensions, same portal, but the family that carries no mark.
        # Subtracting one here would be visible damage to a clean photo.
        ("squareyards", "https://img.squareyards.com/secondaryPortal/optImages/x.jpg"),
        ("magicbricks", "https://img.staticmb.com/mbimages/project/x.jpg"),
    ])
    def test_unmarked_url_family_is_untouched(self, source: str, url: str) -> None:
        img = self._img()
        result = remove_watermark(img, source, url)
        assert result.removed is None
        assert np.array_equal(result.image, img)

    def test_unknown_size_declines_rather_than_guessing(self) -> None:
        # Corrupting a photo is strictly worse than leaving a faint mark, so
        # anything the calibration cannot vouch for must pass through as-is.
        img = self._img(w=37, h=29)
        result = remove_watermark(img, "magicbricks")
        assert result.removed is not True
        assert np.array_equal(result.image, img)

    def test_inversion_recovers_a_synthetic_blend(self) -> None:
        """The whole premise: an alpha blend is exactly invertible."""
        original = self._img(120, 60).astype(np.float32)
        alpha = np.zeros((60, 120, 3), np.float32)
        alpha[20:40, 30:90] = 0.2
        blended = (1 - alpha) * original + alpha * 255.0
        recovered = (blended - alpha * 255.0) / np.maximum(1 - alpha, 1e-3)
        assert np.abs(recovered - original).max() < 0.5
