"""Explicit supported image-edit contract, not a claim about a reseller's routing."""

from urllib.parse import urlsplit

VERSION = "image-edit-contract-v1"
SIZES = {"1024x1024", "1536x1024", "1024x1536"}


def image_contract(base_url, transport="sync"):
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("图片服务地址必须是无凭据、查询参数的HTTPS地址")
    if transport not in {"sync", "timicc_async"}:
        raise ValueError("Unsupported image transport")
    if transport == "timicc_async" and (
        parsed.hostname != "timicc.com" or parsed.path.rstrip("/") != "/v1"
    ):
        raise ValueError("Async image transport requires the verified TiMi endpoint")
    contract = {
        "version": VERSION,
        "endpoint": base_url.rstrip("/") + "/images/edits",
        "requested_model": "gpt-image-2",
        "upstream_model_verified": False,
        "sizes": sorted(SIZES),
        "max_images": 5,
        "response": "b64_json",
        "size_mismatch_policy": "preserve_original_needs_review",
    }
    if transport == "timicc_async":
        contract.update(transport=transport, endpoint=contract["endpoint"] + "/async")
    return contract


def output_checks(expected_size, dimensions):
    expected = [int(n) for n in expected_size.split("x")]
    matched = dimensions == expected
    return {
        "expected_dimensions": expected,
        "dimensions": dimensions,
        "dimensions_match": matched,
        "issues": []
        if matched
        else [
            f"输出尺寸{dimensions[0]}×{dimensions[1]}不符合请求{expected[0]}×{expected[1]}；保留原图，未自动缩放。"
        ],
    }
