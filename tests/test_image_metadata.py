import unittest

from app.image_metadata import validate_image_inputs


class ImageMetadataTests(unittest.TestCase):
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
            )


if __name__ == "__main__":
    unittest.main()
