#!/usr/bin/env python3
"""
生成30张带有日常自然出现文字的图片
用于OCR文字识别评测
文字是日常场景中自然出现的，不是特意摆拍
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
NUM_THREADS = 3

print_lock = threading.Lock()
progress = {"done": 0, "success": 0, "total": 0}

# 30张带日常文字的图片
# 格式: (文件名, 提示词, 中文描述, 场景, 期望识别出的文字列表, 物体列表, 人数)
TEXT_PHOTOS = [
    # ===== 街头/路牌 =====
    (
        "p-109_stop_sign.jpg",
        "Red octagonal STOP traffic sign at street intersection, white capital letters 'STOP' clearly visible on red background, stop sign on metal pole, suburban street, daytime, realistic photo",
        "STOP停车标志",
        "户外",
        ["STOP"],
        ["路牌", "停车标志"],
        0,
    ),
    (
        "p-110_no_smoking.jpg",
        "No smoking sign on restaurant wall, red circle with diagonal line over cigarette icon, black text 'NO SMOKING' below the symbol, indoor wall, public place notice sign",
        "禁止吸烟标识",
        "室内",
        ["NO SMOKING"],
        ["标识牌"],
        0,
    ),
    (
        "p-111_street_sign.jpg",
        "Blue street sign on pole at intersection, white Chinese characters '中山路' (Zhongshan Road) on blue background, also English 'ZHONGSHAN LU', street corner daytime, traffic sign",
        "中山路路牌",
        "街道",
        ["中山路", "ZHONGSHAN LU"],
        ["路牌"],
        0,
    ),
    (
        "p-112_exit_sign.jpg",
        "Green illuminated EXIT sign above door in building, white text 'EXIT' and running person pictogram, glowing green emergency exit sign, indoor public building, safety sign",
        "安全出口标识",
        "室内",
        ["EXIT"],
        ["安全出口", "标识牌"],
        0,
    ),
    # ===== 餐饮/菜单 =====
    (
        "p-113_coffee_cup.jpg",
        "Disposable paper coffee cup from Starbucks on wooden table, green mermaid logo visible, side of cup showing text, coffee shop table, morning natural light",
        "星巴克咖啡杯",
        "餐厅",
        ["Starbucks"],
        ["咖啡杯"],
        0,
    ),
    (
        "p-114_restaurant_menu.jpg",
        "Chinese restaurant menu open on table, dishes listed with prices in Chinese, visible Chinese text includes '宫保鸡丁 ¥38' '麻婆豆腐 ¥28' '米饭 ¥3', paper menu, restaurant table setting",
        "餐厅菜单",
        "餐厅",
        ["宫保鸡丁", "麻婆豆腐", "米饭"],
        ["菜单"],
        0,
    ),
    (
        "p-115_coca_cola.jpg",
        "Classic red Coca-Cola can on table, white script logo 'Coca-Cola' clearly visible on red aluminum can, condensation droplets, cold drink",
        "可口可乐罐",
        "室内",
        ["Coca-Cola"],
        ["可乐罐", "饮料"],
        0,
    ),
    (
        "p-116_mcdonalds.jpg",
        "McDonald's golden arches M sign visible through car window or from street, restaurant exterior, yellow M logo on red background, fast food restaurant building",
        "麦当劳招牌",
        "户外",
        ["M"],
        ["麦当劳", "招牌"],
        0,
    ),
    # ===== 商品包装 =====
    (
        "p-117_milk_carton.jpg",
        "Carton of Yili pure milk on kitchen table, blue and white packaging, Chinese text '伊利纯牛奶' clearly visible, milk carton packaging, breakfast table",
        "伊利纯牛奶盒",
        "室内",
        ["伊利纯牛奶"],
        ["牛奶盒"],
        0,
    ),
    (
        "p-118_noodle_cup.jpg",
        "Cup Noodles instant noodle cup on desk, red packaging with white text 'Cup Noodles', open cup with chopsticks resting on edge, office desk quick meal",
        "合味道杯面",
        "室内",
        ["Cup Noodles"],
        ["泡面杯"],
        0,
    ),
    (
        "p-119_water_bottle.jpg",
        "Plastic water bottle of Nongfu Spring on table, red cap, label with Chinese text '农夫山泉' visible, clear water bottle",
        "农夫山泉矿泉水",
        "室内",
        ["农夫山泉"],
        ["矿泉水瓶"],
        0,
    ),
    (
        "p-120_chocolate_bar.jpg",
        "Hershey's chocolate bar partially unwrapped on table, silver wrapper with brown text 'HERSHEY'S' visible, milk chocolate bar",
        "好时巧克力",
        "室内",
        ["HERSHEY'S"],
        ["巧克力"],
        0,
    ),
    # ===== 屏幕/电子设备 =====
    (
        "p-121_phone_lock.jpg",
        "Close up of iPhone lock screen showing time, digital clock displaying '9:41', date text, phone on table, screen lit up, smartphone display",
        "手机锁屏显示时间",
        "室内",
        ["9:41"],
        ["手机"],
        0,
    ),
    (
        "p-122_laptop_keyboard.jpg",
        "Laptop on office desk with screen showing code or text, keyboard in foreground, screen has visible text 'Hello World' in editor, programmer workspace",
        "笔记本电脑屏幕显示Hello World",
        "室内",
        ["Hello World"],
        ["笔记本电脑"],
        0,
    ),
    (
        "p-123_tv_remote.jpg",
        "TV remote control on couch, buttons with text labels, visible button text includes 'POWER' 'VOL' 'CH', television remote control on sofa",
        "电视遥控器",
        "室内",
        ["POWER"],
        ["遥控器"],
        0,
    ),
    # ===== 书籍/印刷品 =====
    (
        "p-124_book_cover.jpg",
        "Book on desk, cover showing Chinese title '活着' by Yu Hua, book standing upright, white and black cover design, literature book on wooden desk",
        "《活着》书籍封面",
        "室内",
        ["活着"],
        ["书"],
        0,
    ),
    (
        "p-125_newspaper.jpg",
        "Newspaper open on table, Chinese newspaper with headlines, visible large headline text '人民日报' at top, dated newspaper, breakfast table reading",
        "人民日报报纸",
        "室内",
        ["人民日报"],
        ["报纸"],
        0,
    ),
    (
        "p-126_notebook_handwriting.jpg",
        "Open notebook with handwritten Chinese notes on paper, handwritten text includes '会议纪要' '待办事项' at top, blue ballpoint pen writing, lined notebook page",
        "手写笔记本会议纪要",
        "室内",
        ["会议纪要", "待办事项"],
        ["笔记本"],
        0,
    ),
    # ===== 票据/文件 =====
    (
        "p-127_train_ticket.jpg",
        "Chinese high-speed rail train ticket on table, blue paper ticket, visible text includes '北京南' '上海虹桥' 'G1' departure and destination, train ticket",
        "高铁票北京到上海",
        "室内",
        ["北京南", "上海虹桥"],
        ["火车票"],
        0,
    ),
    (
        "p-128_receipt.jpg",
        "Supermarket shopping receipt on table, printed thermal paper, visible store name '沃尔玛超市' at top, itemized list, receipt from grocery shopping",
        "沃尔玛超市购物小票",
        "室内",
        ["沃尔玛超市"],
        ["购物小票"],
        0,
    ),
    (
        "p-129_boarding_pass.jpg",
        "Airline boarding pass on airport table, white paper ticket, visible text 'BOARDING PASS' at top, flight information, airline ticket",
        "登机牌",
        "室内",
        ["BOARDING PASS"],
        ["登机牌"],
        0,
    ),
    # ===== 店铺招牌 =====
    (
        "p-130_starbucks_sign.jpg",
        "Starbucks coffee shop storefront sign, green circular mermaid logo with 'STARBUCKS COFFEE' text, store exterior, street level shop sign",
        "星巴克门店招牌",
        "户外",
        ["STARBUCKS", "COFFEE"],
        ["招牌"],
        0,
    ),
    (
        "p-131_pharmacy_sign.jpg",
        "Green cross pharmacy sign on street, white Chinese characters '药店' below green cross, drug store sign at night with lights on",
        "药店招牌",
        "街道",
        ["药店"],
        ["招牌", "药店"],
        0,
    ),
    (
        "p-132_convenience_store.jpg",
        "7-Eleven convenience store illuminated sign at night, orange red and green logo with '7-ELEVEN' text, open 24 hours store front, city street night",
        "711便利店招牌",
        "街道",
        ["7-ELEVEN"],
        ["招牌", "便利店"],
        0,
    ),
    # ===== 其他日常文字 =====
    (
        "p-133_calendar.jpg",
        "Desk calendar on office desk showing month, visible large date number '15' and month text '八月 2026' (August 2026), Chinese calendar on desk",
        "日历显示8月15日",
        "室内",
        ["八月", "2026"],
        ["日历"],
        0,
    ),
    (
        "p-134_tshirt_text.jpg",
        "Person wearing white t-shirt with black text 'I ❤️ NY' printed on chest, casual clothing, person visible from chest down, street photo",
        "T恤上印I love NY",
        "户外",
        ["I", "NY"],
        ["T恤"],
        1,
    ),
    (
        "p-135_mug_text.jpg",
        "Ceramic coffee mug on office desk with text 'WORLD'S BEST BOSS' printed in black on white mug, office desk with the mug, boss gift mug",
        "马克杯写着WORLD'S BEST BOSS",
        "室内",
        ["WORLD'S BEST BOSS"],
        ["马克杯"],
        0,
    ),
    (
        "p-136_fridge_magnets.jpg",
        "Refrigerator door covered with magnets and notes, one white paper note stuck on with magnet saying '买牛奶' (buy milk) handwritten in Chinese, kitchen fridge",
        "冰箱贴便签写着买牛奶",
        "室内",
        ["买牛奶"],
        ["冰箱贴", "便签"],
        0,
    ),
    (
        "p-137_price_tag.jpg",
        "Price tag hanging on clothing in store, white tag with black text '¥199' price, clothing retail price tag on shirt in shop",
        "衣服价格标签¥199",
        "室内",
        ["¥199"],
        ["价格标签"],
        0,
    ),
    (
        "p-138_welcome_mat.jpg",
        "Welcome doormat at front door entrance, coir mat with text 'WELCOME' printed in large black letters, front door entrance mat, home entryway",
        "门垫写着WELCOME",
        "室内",
        ["WELCOME"],
        ["门垫"],
        0,
    ),
]


def gen_one(photo_tuple):
    filename, prompt, desc, scene, expected_text, objects, persons = photo_tuple
    save_path = OUTPUT_DIR / filename

    if save_path.exists():
        with print_lock:
            print(f"⏭️  SKIP {filename}", flush=True)
        progress["done"] += 1
        return True

    url = f"{BASE_URL}/images/generations"
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

    # 在提示词中强调文字要清晰可辨
    full_prompt = (
        prompt
        + ", photorealistic, natural everyday photo, clear readable text, high quality, sharp focus on text"
    )

    payload = {
        "model": "gpt-image-2",
        "prompt": full_prompt,
        "n": 1,
        "size": "1024x1024",
        "quality": "high",  # 用high质量让文字更清晰
        "response_format": "b64_json",
    }

    for attempt in range(4):
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
                    print(
                        f"   ✅ {filename} OK ({len(img_bytes)//1024}KB) 文字: {expected_text}",
                        flush=True,
                    )
                    progress["success"] += 1
                    progress["done"] += 1
                return True
            else:
                with print_lock:
                    print(f"   ❌ HTTP {r.status_code}: {r.text[:150]}", flush=True)
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
    to_generate = [p for p in TEXT_PHOTOS if not (OUTPUT_DIR / p[0]).exists()]
    progress["total"] = len(to_generate)

    print("=" * 70)
    print(f"日常文字图片生成 - {NUM_THREADS}线程 - 用于OCR评测")
    print(f"待生成: {len(to_generate)} 张")
    print("=" * 70)
    print()

    if not to_generate:
        print("所有图片已存在")
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
