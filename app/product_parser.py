import json
from typing import Any, Mapping


def parse_product_data(event_data: Mapping[str, Any], selected_product_id: str) -> dict[str, Any]:
    """
    공구 Raw JSON(이벤트 단위)에서 선택된 단품 상품 정보를 추출하고,
    누락된 셀링 포인트 및 부가 정보를 상위 데이터(인스타 캡션, 큐레이터 피치)에서 보강합니다.
    """
    # 1. 대상 단품 상품(product) 탐색
    products = event_data.get("products", [])
    target_product = next((p for p in products if p.get("product_id") == selected_product_id), None)
    
    if not target_product:
        raise ValueError(f"해당 product_id({selected_product_id})를 찾을 수 없습니다.")

    # 2. 가격 및 할인율 텍스트 정량화 (LLM 계산 오류 방지)
    consumer_price = target_product.get("consumer_price")
    sale_price = target_product.get("base_sale_price")
    discount_rate = target_product.get("discount_rate_derived")

    if consumer_price and sale_price:
        price_info = f"정가 {consumer_price:,}원 -> 공구가 {sale_price:,}원 ({discount_rate}% 할인)"
    elif sale_price:
        price_info = f"공구가 {sale_price:,}원"
    else:
        price_info = "공동구매 특별 할인가"

    # 3. 셀링 포인트(USP) Fallback 추출
    # [우선순위] 단품 selling_point -> 단품 usp -> 단품 curator_pitch -> 상위 인스타 캡션
    social_posts = event_data.get("social_posts", [])
    first_caption = social_posts[0].get("caption", "") if social_posts else ""
    
    # 인스타 캡션이 길 경우 핵심 앞부분 추출
    cleaned_caption = first_caption.strip().replace("\n", " ")[:150] if first_caption else ""

    selling_point = (
        target_product.get("selling_point")
        or target_product.get("usp")
        or target_product.get("curator_pitch")
        or (f"{cleaned_caption}..." if cleaned_caption else "공동구매 인기 추천 상품")
    )

    # 4. 정제된 핵심 딕셔너리 구성 (불필요한 detail_image_urls, 빈 배열 제거)
    refined_data: dict[str, Any] = {
        "name": target_product.get("name"),
        "option1": target_product.get("option1"),
        "option2": target_product.get("option2"),
        "price_info": price_info,
        "key_selling_point": selling_point,
    }

    # 5. 네이버 평점/리뷰 정보가 있는 경우에만 포함
    naver_rating = target_product.get("naver_rating")
    review_count = target_product.get("naver_review_count")
    if naver_rating and review_count:
        refined_data["rating_info"] = f"네이버 평점 {naver_rating}점 (리뷰 {review_count:,}개)"

    # 6. 실제 사용자 리뷰(Fact)가 있는 경우 대표 리뷰 1건 요약 전달
    reviews = target_product.get("reviews", [])
    if reviews and isinstance(reviews, list) and len(reviews) > 0:
        first_review_body = reviews[0].get("body", "").strip().replace("\n", " ")[:100]
        if first_review_body:
            refined_data["sample_review"] = f"{first_review_body}..."

    return refined_data