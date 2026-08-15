import json
from typing import Any, Mapping


def parse_product_data(event_data: Mapping[str, Any], selected_product_id: str) -> dict[str, Any]:
    """
    공구 Raw JSON에서 선택된 단품 상품 정보를 추출하고 정제합니다.
    - 소거법: LLM이 볼 수 없는 노이즈 컬럼과 빈 값(None, '', [], {})만 제거
    - 보존: structured_specs, hashtags, 리뷰 전체 등 실제 값이 있는 모든 필드 유지
    - 보강: 필수 소구점이 비어있는 경우에만 상위 데이터에서 Fallback
    """
    products = event_data.get("products", [])
    raw_target = next((p for p in products if p.get("product_id") == selected_product_id), None)
    
    if not raw_target:
        raise ValueError(f"해당 product_id({selected_product_id})를 찾을 수 없습니다.")

    # 1. 원본 딕셔너리 복제
    product = dict(raw_target)

    # 2. 가격 정량화 정보 추가 (LLM 계산 오류 방지)
    consumer_price = product.get("consumer_price")
    sale_price = product.get("base_sale_price")
    discount_rate = product.get("discount_rate_derived")

    if consumer_price and sale_price:
        product["price_info"] = f"정가 {consumer_price:,}원 -> 공구가 {sale_price:,}원 ({discount_rate}% 할인)"
    elif sale_price:
        product["price_info"] = f"공구가 {sale_price:,}원"

    # 3. 필수 셀링포인트 Fallback (단품에 아무 소구점도 없을 때만 상위 인스타 캡션 주입)
    social_posts = event_data.get("social_posts", [])
    caption = social_posts[0].get("caption", "") if social_posts else ""
    cleaned_caption = caption.strip().replace("\n", " ")[:150] if caption else ""

    if not product.get("selling_point") and not product.get("usp") and not product.get("curator_pitch"):
        product["key_selling_point"] = f"{cleaned_caption}..." if cleaned_caption else "공동구매 인기 추천 상품"

    # 4. 리뷰 정제 (불필요한 메타데이터 제외하고 실제 작성된 후기 텍스트 전체 보존)
    raw_reviews = product.get("reviews")
    if raw_reviews and isinstance(raw_reviews, list):
        review_texts = [r.get("body", "").strip().replace("\n", " ") for r in raw_reviews if r.get("body")]
        product["reviews"] = review_texts

    # 5. [소거 대상 1] 텍스트 LLM에 무의미한 노이즈 및 시스템 식별자
    EXCLUDED_KEYS = {
        "product_id",
        "category_code",
        "image_url",
        "detail_image_urls"
    }

    # 6. [소거 대상 2] 제외 대상 키 + 빈 값(None, 빈 문자열, 빈 리스트, 빈 딕셔너리) 자동 제거
    cleaned_product = {}
    for key, val in product.items():
        if key in EXCLUDED_KEYS:
            continue
        if val is None or val == "" or val == [] or val == {}:
            continue
        cleaned_product[key] = val

    return cleaned_product


# =====================================================================
# 실제 공구 JSON 파일 로컬 단독 테스트
# =====================================================================
if __name__ == "__main__":
    with open("quedot-gonggu-sample.json", "r", encoding="utf-8") as f:
        full_data = json.load(f)

    # 비나맘 X 프랭클린 이벤트 -> '주방세제 리필 3개' 단품 테스트
    binamom_event = full_data["events"][0]
    target_id = "eb1aae5c-24bf-4eb6-8b23-c9860fb75771"

    refined_result = parse_product_data(binamom_event, target_id)
    print(json.dumps(refined_result, ensure_ascii=False, indent=2))