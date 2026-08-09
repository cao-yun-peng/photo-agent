"""为 OSS Bucket 配置 CORS 规则，允许小程序/H5 前端浏览器发起直传 PUT。

用法：
  在 photo-agent 项目根，激活 venv 后执行
      python scripts/setup_oss_cors.py

前提：
  .env 已填好真实的 OSS_KEY_ID / OSS_KEY_SECRET / OSS_BUCKET / OSS_ENDPOINT。

生效范围：只对当前 Bucket 生效，一次性动作，改了再跑即可覆盖。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 让脚本无需装包也能跑：加项目根到 sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import oss2  # noqa: E402
from oss2.models import CorsRule  # noqa: E402

from app.config import settings  # noqa: E402


def main() -> None:
    if not settings.oss_key_id or settings.oss_key_id == "LTAI_xxx":
        print("!! 未检测到真实 OSS_KEY_ID，请先在 .env 里填好，再运行。")
        sys.exit(1)

    auth = oss2.Auth(settings.oss_key_id, settings.oss_key_secret)
    bucket = oss2.Bucket(auth, f"https://{settings.oss_endpoint}", settings.oss_bucket)

    rule = CorsRule(
        # 生产环境改成自家域名白名单，例如 ["https://app.example.com"]
        allowed_origins=["*"],
        allowed_methods=["PUT", "GET", "HEAD"],
        allowed_headers=["*"],
        expose_headers=["ETag", "x-oss-request-id"],
        max_age_seconds=600,
    )
    bucket.put_bucket_cors(oss2.models.BucketCors([rule]))
    print(f"✓ CORS 配置已写入 bucket={settings.oss_bucket}")
    print("  Allowed-Origins: *")
    print("  Allowed-Methods: PUT / GET / HEAD")


if __name__ == "__main__":
    main()
