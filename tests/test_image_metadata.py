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

        self.assertIsNone(result)
        self.assertEqual(
            seen,
            [
                "https://example.com/product.jpg",
                "https://example.com/influencer.jpg",
                "https://example.com/detail.jpg",
            ],
        )

    def test_rejects_any_invalid_image_with_its_label(self):
        def read_dimensions(url):
            if url.endswith("detail.jpg"):
                return 860, 1
            return 800, 1200

        with self.assertRaisesRegex(ValueError, "상세 이미지 1번째.*860x1"):
            validate_image_inputs(
                image_url="https://example.com/product.jpg",
                detail_image_urls=("https://example.com/detail.jpg",),
                dimensions_reader=read_dimensions,
            )


if __name__ == "__main__":
    unittest.main()
