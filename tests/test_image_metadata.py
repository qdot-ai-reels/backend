import unittest
from unittest.mock import patch

from app.image_metadata import (
    validate_image_inputs,
    validate_normalized_influencer_references,
    validate_remote_image_url,
)


class ImageMetadataTests(unittest.TestCase):
    def tearDown(self):
        validate_remote_image_url.cache_clear()

    def test_rejects_non_https_and_private_image_targets(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            validate_remote_image_url("http://example.com/image.jpg")
        with patch(
            "app.image_metadata.socket.getaddrinfo",
            return_value=[(None, None, None, None, ("127.0.0.1", 443))],
        ), self.assertRaisesRegex(ValueError, "사설"):
            validate_remote_image_url("https://internal.example/image.jpg")

    def test_enforces_optional_image_host_allowlist(self):
        with patch.dict("os.environ", {"ALLOWED_IMAGE_HOSTS": "cdn.example.com"}), patch(
            "app.image_metadata.socket.getaddrinfo",
            return_value=[(None, None, None, None, ("93.184.216.34", 443))],
        ):
            with self.assertRaisesRegex(ValueError, "허용되지 않은"):
                validate_remote_image_url("https://other.example.com/image.jpg")

    def test_accepts_public_https_image_target(self):
        with patch.dict("os.environ", {"ALLOWED_IMAGE_HOSTS": ""}), patch(
            "app.image_metadata.socket.getaddrinfo",
            return_value=[(None, None, None, None, ("93.184.216.34", 443))],
        ):
            self.assertEqual(
                validate_remote_image_url("https://cdn.example.com/image.jpg"),
                "https://cdn.example.com/image.jpg",
            )

    def test_rejects_landscape_influencer_contact_sheet(self):
        with self.assertRaisesRegex(ValueError, "콘택트시트"):
            validate_normalized_influencer_references(
                ("https://example.com/contact-sheet.png",),
                dimensions_reader=lambda _url: (1142, 857),
            )

    def test_validates_all_required_image_inputs(self):
        seen = []

        def read_dimensions(url):
            seen.append(url)
            return 800, 1200

        result = validate_image_inputs(
            image_url="https://example.com/product.jpg",
            influencer_image_url="https://example.com/influencer.jpg",
            detail_image_urls=("https://example.com/detail.jpg",),
            dimensions_reader=read_dimensions,
            format_reader=lambda _url: "jpg",
        )

        self.assertEqual(result, ("https://example.com/detail.jpg",))
        self.assertEqual(
            seen,
            [
                "https://example.com/product.jpg",
                "https://example.com/influencer.jpg",
                "https://example.com/detail.jpg",
            ],
        )

    def test_skips_invalid_detail_image(self):
        def read_dimensions(url):
            if url.endswith("/detail.jpg"):
                return 860, 1
            return 800, 1200

        result = validate_image_inputs(
            image_url="https://example.com/product.jpg",
            detail_image_urls=(
                "https://example.com/detail.jpg",
                "https://example.com/valid-detail.jpg",
            ),
            dimensions_reader=read_dimensions,
            format_reader=lambda _url: "jpg",
        )

        self.assertEqual(result, ("https://example.com/valid-detail.jpg",))

    def test_still_rejects_invalid_required_image(self):
        def read_dimensions(url):
            if url.endswith("product.jpg"):
                return 860, 1
            return 800, 1200

        with self.assertRaisesRegex(ValueError, "860x1"):
            validate_image_inputs(
                image_url="https://example.com/product.jpg",
                dimensions_reader=read_dimensions,
                format_reader=lambda _url: "jpg",
            )

    def test_skips_unsupported_detail_image_format(self):
        result = validate_image_inputs(
            image_url="https://example.com/product.jpg",
            detail_image_urls=(
                "https://example.com/detail.gif",
                "https://example.com/detail.webp",
            ),
            dimensions_reader=lambda _url: (800, 1200),
            format_reader=lambda url: "gif" if url.endswith(".gif") else "webp",
        )

        self.assertEqual(result, ("https://example.com/detail.webp",))

    def test_still_rejects_unsupported_required_image_format(self):
        with self.assertRaisesRegex(ValueError, "gif.*지원되지 않습니다"):
            validate_image_inputs(
                image_url="https://example.com/product.gif",
                dimensions_reader=lambda _url: (800, 1200),
                format_reader=lambda _url: "gif",
            )


if __name__ == "__main__":
    unittest.main()
