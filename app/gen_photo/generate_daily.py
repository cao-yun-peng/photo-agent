#!/usr/bin/env python3
"""
日常生活照片生成器 - 5线程并发
25张非常贴近真实生活场景的照片
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
NUM_THREADS = 5

print_lock = threading.Lock()
progress = {"done": 0, "success": 0, "total": 0}

# 日常生活场景照片
DAILY_PHOTOS = [
    # ===== 早晨/早餐 =====
    (
        "p-057_breakfast_toast.jpg",
        "Morning breakfast table, toast with fried egg and avocado, cup of orange juice, morning sunlight through kitchen window, cozy home breakfast, smartphone on table",
        "早餐吐司煎蛋",
        "室内",
        ["早餐", "食物", "早晨"],
    ),
    (
        "p-058_making_coffee.jpg",
        "Person pouring coffee from moka pot into cup in kitchen, morning routine, steam rising, home kitchen, warm light",
        "在家煮咖啡",
        "室内",
        ["咖啡", "厨房", "早晨"],
    ),
    # ===== 通勤/出行 =====
    (
        "p-059_subway.jpg",
        "Inside subway metro train, commuters looking at phones, morning commute, urban public transportation, window view blurred",
        "地铁通勤",
        "室内",
        ["地铁", "通勤", "城市"],
    ),
    (
        "p-060_bus_window.jpg",
        "View from bus window on rainy day, raindrops on glass, city street passing by, blurry urban scenery, commute mood",
        "公交车窗外雨景",
        "室内",
        ["公交", "雨", "通勤"],
    ),
    (
        "p-061_walking_dog.jpg",
        "Person walking golden retriever dog on leash in residential neighborhood, autumn sidewalk, trees with fallen leaves, evening light",
        "小区遛狗",
        "户外",
        ["遛狗", "散步", "日常"],
    ),
    # ===== 购物 =====
    (
        "p-062_supermarket.jpg",
        "Person pushing shopping cart in supermarket aisle, shelves full of groceries products, bright fluorescent lighting, grocery shopping",
        "超市购物",
        "室内",
        ["超市", "购物", "日常"],
    ),
    (
        "p-063_fruit_stand.jpg",
        "Fresh fruit stand at local outdoor market, colorful apples oranges bananas, vendor arranging fruits, street market",
        "水果摊",
        "户外",
        ["水果", "市场", "购物"],
    ),
    (
        "p-064_clothes_store.jpg",
        "Person browsing clothes on rack in clothing store, shopping mall, casual shopping",
        "服装店挑衣服",
        "室内",
        ["购物", "商场"],
    ),
    # ===== 餐饮/聚会 =====
    (
        "p-065_hotpot_friends.jpg",
        "Group of friends eating hot pot together at restaurant, steam rising from pot, laughing and talking, chopsticks reaching for food, dinner gathering",
        "朋友聚餐吃火锅",
        "餐厅",
        ["聚餐", "朋友", "火锅"],
    ),
    (
        "p-066_takeout.jpg",
        "Chinese takeout food containers on coffee table at home, watching TV, lazy evening meal, disposable chopsticks, casual dinner",
        "在家吃外卖",
        "室内",
        ["外卖", "晚餐", "宅家"],
    ),
    (
        "p-067_barbecue.jpg",
        "Backyard barbecue grill with meat skewers cooking, friends gathered around, summer evening outdoor party, smoke from grill",
        "户外烧烤",
        "户外",
        ["烧烤", "聚会", "夏天"],
    ),
    # ===== 居家/休闲 =====
    (
        "p-068_tv_couch.jpg",
        "Person lying on couch watching TV at home, blanket over legs, bowl of snacks on lap, evening relaxation, living room",
        "沙发看电视",
        "室内",
        ["电视", "沙发", "休闲"],
    ),
    (
        "p-069_doing_laundry.jpg",
        "Loading washing machine with clothes at home, laundry basket, household chores, laundry room",
        "用洗衣机洗衣服",
        "室内",
        ["家务", "洗衣"],
    ),
    (
        "p-070_balcony_plants.jpg",
        "Small balcony garden with potted plants herbs, watering can, morning sunlight, apartment balcony view",
        "阳台浇花",
        "室内",
        ["阳台", "植物", "早晨"],
    ),
    (
        "p-071_napping_cat.jpg",
        "Cat sleeping curled up on laptop keyboard, home office funny moment, cute pet interrupting work",
        "猫趴在键盘上睡觉",
        "室内",
        ["猫", "宠物", "办公"],
    ),
    # ===== 运动/健身 =====
    (
        "p-072_gym_workout.jpg",
        "Person lifting dumbbells at gym, workout exercise, fitness training, gym equipment in background",
        "健身房举铁",
        "室内",
        ["健身", "运动", "健身房"],
    ),
    (
        "p-073_jogging_park.jpg",
        "Person jogging running in park, morning exercise, athletic wear, trees and path, healthy lifestyle",
        "公园跑步",
        "户外",
        ["跑步", "运动", "公园"],
    ),
    # ===== 学习/工作 =====
    (
        "p-074_studying.jpg",
        "Student studying at desk with open books and laptop, late night, desk lamp, coffee cup, focused learning",
        "书桌前学习",
        "室内",
        ["学习", "书桌", "夜晚"],
    ),
    (
        "p-075_video_call.jpg",
        "Person on video call on laptop, work from home, waving at screen, home office, remote meeting",
        "视频会议通话",
        "室内",
        ["工作", "电脑", "远程"],
    ),
    (
        "p-076_writing_notes.jpg",
        "Hand writing notes in notebook with pen, close up of writing, study or journaling, paper notebook",
        "手写笔记",
        "室内",
        ["笔记", "学习", "书写"],
    ),
    # ===== 休闲娱乐 =====
    (
        "p-077_movie_theater.jpg",
        "Inside dark movie theater cinema, bright movie screen, silhouettes of audience watching film, popcorn bucket",
        "电影院看电影",
        "室内",
        ["电影", "影院", "娱乐"],
    ),
    (
        "p-078_convenience_store.jpg",
        "Inside 24-hour convenience store at night, person paying at counter, refrigerated drinks, late night snack",
        "深夜便利店",
        "室内",
        ["便利店", "夜晚", "购物"],
    ),
    (
        "p-079_umbrella_rain.jpg",
        "Person holding colorful umbrella walking in rain on city sidewalk, rainy day, wet pavement reflections, umbrella close up",
        "雨中撑伞走路",
        "户外",
        ["雨", "伞", "走路"],
    ),
    # ===== 亲子/家庭 =====
    (
        "p-080_child_drawing.jpg",
        "Little child drawing with crayons on paper at table, hands close up, colorful art, creative kids activity",
        "小孩画画",
        "室内",
        ["孩子", "画画", "家庭"],
    ),
    # ===== 睡前 =====
    (
        "p-081_reading_bed.jpg",
        "Person reading book in bed before sleep, warm bedside lamp light, cozy bedroom, night time reading",
        "睡前床上看书",
        "室内",
        ["阅读", "床", "夜晚"],
    ),
]


def gen_one(photo_tuple):
    filename, prompt, desc, scene, tags = photo_tuple
    save_path = OUTPUT_DIR / filename

    if save_path.exists():
        with print_lock:
            print(f"⏭️  SKIP {filename}", flush=True)
        progress["done"] += 1
        return True

    url = f"{BASE_URL}/images/generations"
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

    size = "1024x1024"
    full_prompt = (
        prompt
        + ", candid smartphone photo style, natural lighting, realistic everyday moment, high quality, photorealistic"
    )

    payload = {
        "model": "gpt-image-2",
        "prompt": full_prompt,
        "n": 1,
        "size": size,
        "quality": "medium",
        "response_format": "b64_json",
    }

    for attempt in range(3):
        try:
            if attempt > 0:
                time.sleep(4 * attempt)

            with print_lock:
                print(
                    f"🎨 [{progress['done']+1}/{progress['total']}] {filename} - {desc}",
                    flush=True,
                )

            r = requests.post(url, headers=headers, json=payload, timeout=180)

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
                print(f"   ❌ {type(e).__name__} attempt {attempt+1}", flush=True)

    with print_lock:
        print(f"   💀 FAILED: {filename}", flush=True)
        progress["done"] += 1
    return False


def main():
    if not API_KEY or not BASE_URL:
        print("请先设置 IMAGE_API_KEY 和 IMAGE_API_BASE_URL", file=sys.stderr)
        return 2
    progress["total"] = len(DAILY_PHOTOS)

    print("=" * 70)
    print(f"日常生活照片生成 - {NUM_THREADS}线程并发")
    print(f"数量: {len(DAILY_PHOTOS)} 张")
    print("=" * 70)
    print()

    start = time.time()

    with ThreadPoolExecutor(max_workers=NUM_THREADS) as ex:
        futures = [ex.submit(gen_one, p) for p in DAILY_PHOTOS]
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
