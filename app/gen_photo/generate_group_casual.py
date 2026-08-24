#!/usr/bin/env python3
"""
群像照片 + 日常随手拍（手机抓拍感）- 3线程稳定版
"""

import sys
import time
import base64
import os
import requests
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

API_KEY = os.getenv("IMAGE_API_KEY", "")
BASE_URL = os.getenv("IMAGE_API_BASE_URL", "")
OUTPUT_DIR = Path(
    os.getenv("PHOTO_TEST_OUTPUT_DIR", Path(__file__).parents[1] / "test_photos")
)
OUTPUT_DIR.mkdir(exist_ok=True)
NUM_THREADS = 3  # 降低到3线程更稳定

print_lock = threading.Lock()
progress = {"done": 0, "success": 0, "total": 0}

PHOTOS = [
    # ===== 群像照片（多人） =====
    (
        "p-082_friends_selfie.jpg",
        "Group of young friends taking selfie together at restaurant table, everyone smiling making funny faces at phone camera, holding up drinks glasses, casual phone selfie high angle, warm indoor restaurant lighting, candid happy moment, realistic smartphone photo",
        "朋友餐厅自拍合影",
        "群像",
        "室内",
        ["朋友", "聚餐", "自拍"],
    ),
    (
        "p-084_wedding_group.jpg",
        "Wedding group photo outdoors in garden, bride in white dress and groom in suit surrounded by smiling family and wedding party, everyone posing together, sunny day, happy wedding celebration",
        "婚礼亲友合影",
        "群像",
        "户外",
        ["婚礼", "合影", "家庭"],
    ),
    (
        "p-086_birthday_group.jpg",
        "Group of friends gathered around birthday cake with lit candles, everyone singing happy birthday, warm indoor party lighting, faces glowing from candlelight, surprise celebration moment, candid photo",
        "生日惊喜聚会",
        "群像",
        "室内",
        ["生日", "朋友", "聚会"],
    ),
    (
        "p-087_class_photo.jpg",
        "Vintage early 2000s school class photo, students in uniforms lined up in rows in front of school building, slightly faded nostalgic film look, traditional class group photograph",
        "班级毕业合影",
        "群像",
        "户外",
        ["同学", "老照片", "合影"],
    ),
    (
        "p-088_hiking_group.jpg",
        "Group of happy hikers at mountain summit, backpacks and trekking poles, beautiful mountain view behind them, arms around each other, smiling tired but proud, achievement celebration",
        "登山团队山顶合影",
        "群像",
        "户外",
        ["徒步", "团队", "山"],
    ),
    (
        "p-089_concert_crowd.jpg",
        "Wide shot of crowd at outdoor music festival, thousands of people with hands up enjoying concert, stage lights in distance, summer music festival atmosphere, audience enjoying live music together",
        "演唱会观众群像",
        "群像",
        "户外",
        ["演唱会", "人群", "音乐"],
    ),
    (
        "p-090_family_dinner.jpg",
        "Big multi-generation Chinese family dinner around large round table, lazy susan in center with many dishes, everyone eating and talking warmly, Spring Festival reunion dinner feeling, cozy restaurant",
        "家庭团圆聚餐",
        "群像",
        "餐厅",
        ["家庭", "聚餐", "团圆"],
    ),
    (
        "p-091_bridesmaids.jpg",
        "Bride and bridesmaids in matching robes getting ready together in hotel room before wedding, laughing while doing hair and makeup, champagne glasses, happy wedding preparation candid moment",
        "伴娘团婚礼准备",
        "群像",
        "室内",
        ["婚礼", "闺蜜", "化妆"],
    ),
    (
        "p-092_sports_team.jpg",
        "Happy soccer team group photo on green field after winning game, players in red and blue jerseys holding up trophy together, arms around each other, smiling victory celebration, grass field",
        "球队获胜合影",
        "群像",
        "户外",
        ["运动", "团队", "足球"],
    ),
    (
        "p-093_roommates.jpg",
        "Group of college roommates hanging out in cozy messy dorm room, sitting on beds and floor eating snacks laughing, casual lazy Sunday afternoon together, candid student life, natural lighting",
        "大学室友宿舍合照",
        "群像",
        "室内",
        ["室友", "大学", "宿舍"],
    ),
    # ===== 日常随手拍（手机抓拍感，非专业构图） =====
    (
        "p-094_sneaky_cat.jpg",
        "Blurry quick phone photo of orange cat knocking over a glass of water on kitchen table, mid-action paw hitting glass, water spilling, slightly out of focus because taken in a hurry, funny candid pet moment",
        "抓拍猫打翻杯子",
        "随手拍",
        "室内",
        ["猫", "抓拍", "搞笑"],
    ),
    (
        "p-095_food_photo.jpg",
        "Quick casual phone photo of Chinese food dishes on restaurant table before eating, slightly tilted angle not perfectly composed, typical food photo people send to WeChat group, dishes include stir fry and rice, warm lighting",
        "吃饭前随手拍菜",
        "随手拍",
        "餐厅",
        ["美食", "随手拍"],
    ),
    (
        "p-096_sleeping_dog.jpg",
        "Candid phone photo of golden retriever dog sleeping on back in weird funny position on couch, belly up legs in air, taken quietly to not wake it, slightly dark living room evening light, cute silly pet moment",
        "偷拍狗狗奇葩睡姿",
        "随手拍",
        "室内",
        ["狗", "宠物", "偷拍"],
    ),
    (
        "p-097_rain_out_window.jpg",
        "Quick blurry snap out of office window showing heavy rain pouring down, raindrops streaking on glass, grey overcast sky, taken from desk during work day, typical photo sent to complain about weather to friends",
        "办公室窗外下雨随手拍",
        "随手拍",
        "室内",
        ["雨", "窗户", "办公"],
    ),
    (
        "p-098_feet_travel.jpg",
        "Typical tourist feet photo while traveling, person wearing white sneakers standing on stone path with beautiful mountain lake view in background, casual travel snapshot taken to show location",
        "旅行时拍脚和风景",
        "随手拍",
        "户外",
        ["旅行", "脚", "风景"],
    ),
    (
        "p-099_messy_desk.jpg",
        "Realistic messy cluttered work desk from above, papers scattered, half empty coffee cup, keyboard, post-it notes, cables tangled, not staged at all, photo taken to show friend how busy work is",
        "乱糟糟的办公桌",
        "随手拍",
        "室内",
        ["办公", "桌面", "杂乱"],
    ),
    (
        "p-100_traffic_jam.jpg",
        "Photo from car driver seat showing long line of cars in heavy traffic jam on highway, taken through windshield during evening commute, cars stopped as far as eye can see, red tail lights, frustrated commute moment",
        "堵车时随手拍路况",
        "随手拍",
        "车内",
        ["堵车", "通勤", "交通"],
    ),
    (
        "p-101_sunset_sky.jpg",
        "Quick phone snap of beautiful pink and orange sunset sky through car window while driving home, slightly blurry through glass, amazing colorful clouds, taken because sky looked pretty after work",
        "开车时随手拍日落",
        "随手拍",
        "车内",
        ["日落", "天空", "随手拍"],
    ),
    (
        "p-102_coffee_morning.jpg",
        "First cup of black coffee in morning on wooden kitchen counter, hand holding mug just after waking up, soft morning sunlight through window, first thing after getting up photo, lazy morning vibe",
        "早上第一杯咖啡",
        "随手拍",
        "室内",
        ["咖啡", "早晨", "日常"],
    ),
    (
        "p-103_funny_sign.jpg",
        "Amusing funny street sign noticed while walking, photo taken crookedly to send to group chat, snapshot of weird or humorous sign seen on the street, casual walk discovery",
        "路边有趣的路牌随手拍",
        "随手拍",
        "户外",
        ["路牌", "搞笑", "街头"],
    ),
    (
        "p-104_parking_ticket.jpg",
        "Yellow parking ticket tucked under car windshield wiper, photo taken to complain to friends about bad luck, slightly annoyed perspective, everyday annoying moment snapshot",
        "车上被贴罚单拍下来吐槽",
        "随手拍",
        "户外",
        ["罚单", "车", "吐槽"],
    ),
    (
        "p-105_new_shoes.jpg",
        "Person wearing new white sneakers standing on sidewalk, photo taken looking down at feet to show friends new shoes purchase, excited shopping photo, casual new purchase share",
        "新买的鞋子拍给朋友看",
        "随手拍",
        "户外",
        ["鞋子", "购物", "分享"],
    ),
    (
        "p-106_empty_fridge.jpg",
        "Open refrigerator door at midnight showing almost empty inside, only a few bottles of condiments and beer on shelves, light from fridge illuminating dark kitchen, late night hungry moment before ordering takeout",
        "半夜打开冰箱空空如也",
        "随手拍",
        "室内",
        ["冰箱", "深夜", "外卖"],
    ),
    (
        "p-107_bad_hair_day.jpg",
        "Funny selfie of messy bed head hair in mirror just after waking up in morning, no makeup, squinting at camera, silly face, selfie sent to partner or friend group to joke about bed hair",
        "刚睡醒炸毛自拍",
        "随手拍",
        "室内",
        ["自拍", "早晨", "搞笑"],
    ),
    (
        "p-108_delivery_packages.jpg",
        "Pile of cardboard delivery boxes stacked at front door, many online shopping packages arrived, photo taken of all the boxes to show shopping haul, excited online shopping delivery day",
        "一堆快递到了拍一下",
        "随手拍",
        "室内",
        ["快递", "购物", "日常"],
    ),
]


def gen_one(photo_tuple):
    filename, prompt, desc, category, scene, tags = photo_tuple
    save_path = OUTPUT_DIR / filename

    if save_path.exists():
        with print_lock:
            print(f"⏭️  SKIP {filename}", flush=True)
        progress["done"] += 1
        return True

    url = f"{BASE_URL}/images/generations"
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

    size = "1024x1024"
    full_prompt = prompt

    payload = {
        "model": "gpt-image-2",
        "prompt": full_prompt,
        "n": 1,
        "size": size,
        "quality": "medium",
        "response_format": "b64_json",
    }

    for attempt in range(4):  # 更多重试次数
        try:
            if attempt > 0:
                wait_time = 5 * attempt
                time.sleep(wait_time)

            with print_lock:
                print(
                    f"🎨 [{progress['done']+1}/{progress['total']}] {filename} - {desc} (try {attempt+1})",
                    flush=True,
                )

            r = requests.post(url, headers=headers, json=payload, timeout=200)

            if r.status_code == 200:
                data = r.json()
                img_bytes = base64.b64decode(data["data"][0]["b64_json"])
                with open(save_path, "wb") as f:
                    f.write(img_bytes)
                with print_lock:
                    print(f"   ✅ {filename} OK ({len(img_bytes)//1024}KB)", flush=True)
                    progress["success"] += 1
                    progress["done"] += 1
                return True
            else:
                with print_lock:
                    print(f"   ❌ HTTP {r.status_code}", flush=True)
        except Exception as e:
            with print_lock:
                print(f"   ❌ {type(e).__name__}", flush=True)

    with print_lock:
        print(f"   💀 FAILED: {filename}", flush=True)
        progress["done"] += 1
    return False


def main():
    if not API_KEY or not BASE_URL:
        print("请先设置 IMAGE_API_KEY 和 IMAGE_API_BASE_URL", file=sys.stderr)
        return 2
    # 过滤掉已存在的
    to_generate = [p for p in PHOTOS if not (OUTPUT_DIR / p[0]).exists()]
    progress["total"] = len(to_generate)

    print("=" * 70)
    print(f"群像 + 日常随手拍 生成 - {NUM_THREADS}线程稳定版")
    print(f"待生成: {len(to_generate)} 张")
    print("=" * 70)
    print()

    if not to_generate:
        print("所有图片已存在，无需生成")
        return 0

    start = time.time()

    with ThreadPoolExecutor(max_workers=NUM_THREADS) as ex:
        futures = [ex.submit(gen_one, p) for p in to_generate]
        for f in as_completed(futures):
            f.result()

    elapsed = time.time() - start

    print("\n" + "=" * 70)
    print(f"完成！成功: {progress['success']}/{progress['total']}")
    print(f"耗时: {elapsed/60:.1f} 分钟")

    all_imgs = list(OUTPUT_DIR.glob("p-*.jpg"))
    print(f"📁 test_photos 中共有 {len(all_imgs)} 张图片")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
