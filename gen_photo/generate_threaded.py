#!/usr/bin/env python3
"""
多线程并发扩充测试集生成器 - 5线程并发
扩充更多多样化场景
"""
import sys
import json
import time
import base64
import requests
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# ====================== 配置 ======================
API_KEY = ""
BASE_URL = ""
OUTPUT_DIR = Path(__file__).parent / "test_photos"
OUTPUT_DIR.mkdir(exist_ok=True)
NUM_THREADS = 5
# =================================================

print_lock = threading.Lock()
progress = {"done": 0, "success": 0, "total": 0}

# 扩充测试集 - 更多多样化场景
EXTRA_PHOTOS = [
    # ===== 更多宠物 =====
    ("p-021_puppy.jpg", "Cute golden retriever puppy sitting on grass, looking at camera, adorable fluffy baby dog, sunny day, bokeh background", "金毛小狗", "户外", ["狗", "宠物", "草地"]),
    ("p-022_black_cat.jpg", "Mysterious black cat with yellow eyes sitting on dark background, dramatic lighting, sleek black fur, elegant pose", "黑猫", "室内", ["猫", "宠物"]),
    ("p-023_rabbit.jpg", "Fluffy white pet rabbit eating a carrot in garden, cute bunny, soft focus, spring day", "小白兔", "户外", ["兔子", "宠物"]),
    ("p-024_parrot.jpg", "Colorful scarlet macaw parrot on a branch in tropical forest, bright red and blue feathers, vibrant colors", "金刚鹦鹉", "户外", ["鸟", "宠物"]),
    
    # ===== 更多美食 =====
    ("p-025_chinese_hotpot.jpg", "Chinese Sichuan hot pot with spicy red broth, various ingredients cooking, steam rising, restaurant table, warm lighting", "四川火锅", "餐厅", ["美食", "中餐", "火锅"]),
    ("p-026_sushi.jpg", "Assorted Japanese sushi platter on wooden board, fresh salmon tuna nigiri, maki rolls, wasabi ginger, professional food photography", "寿司拼盘", "餐厅", ["美食", "日本", "寿司"]),
    ("p-027_dessert.jpg", "Beautiful strawberry cake with cream and fresh berries on a white plate, afternoon tea setting, soft natural light", "草莓蛋糕", "室内", ["美食", "甜点", "下午茶"]),
    ("p-028_croissant.jpg", "Freshly baked golden croissant on a bakery counter, flaky pastry, morning sunlight, French cafe", "可颂面包", "餐厅", ["美食", "面包", "早餐"]),
    ("p-029_burger.jpg", "Juicy gourmet cheeseburger with lettuce tomato, melting cheese, sesame bun, wooden table, fast food", "芝士汉堡", "餐厅", ["美食", "汉堡"]),
    ("p-030_dumplings.jpg", "Chinese steamed dumplings jiaozi on bamboo steamer, soy sauce dipping, traditional food", "中式饺子", "餐厅", ["美食", "中餐", "饺子"]),
    
    # ===== 更多风景 =====
    ("p-031_northern_lights.jpg", "Spectacular aurora borealis northern lights over snowy mountains, green purple lights dancing in night sky, magical winter landscape", "北极光", "户外", ["极光", "夜景", "风景"]),
    ("p-032_waterfall.jpg", "Majestic tropical waterfall in lush green rainforest, mist rising, crystal clear water, long exposure smooth effect", "瀑布", "户外", ["瀑布", "森林", "风景"]),
    ("p-033_desert.jpg", "Vast sand dunes in Sahara desert at sunset, golden sand ripples, dramatic shadows, warm orange light", "沙漠日落", "户外", ["沙漠", "日落", "风景"]),
    ("p-034_lavender_field.jpg", "Endless purple lavender field in Provence France, summer, purple flowers stretching to horizon, old farmhouse in distance", "薰衣草田", "户外", ["薰衣草", "花田", "风景"]),
    ("p-035_tulips.jpg", "Colorful tulip field in Netherlands spring, rows of red yellow pink tulips, windmill in background, bright sunny day", "郁金香花田", "户外", ["郁金香", "花田", "春天"]),
    ("p-036_great_wall.jpg", "Great Wall of China winding over green mountains, historic ancient architecture, sunny day, grand landscape", "长城", "户外", ["长城", "古迹", "风景"]),
    
    # ===== 城市/建筑 =====
    ("p-037_shanghai_bund.jpg", "Shanghai Bund skyline at night with Pudong skyscrapers, Huangpu river, bright city lights, modern metropolis", "上海外滩夜景", "户外", ["上海", "夜景", "城市"]),
    ("p-038_ancient_town.jpg", "Traditional Chinese water town Jiangnan style, ancient stone bridges over canal, old buildings, boats on water, peaceful atmosphere", "江南水乡古镇", "户外", ["古镇", "建筑", "江南"]),
    ("p-039_modern_architecture.jpg", "Futuristic modern architecture building with glass and steel, geometric lines, blue sky reflection, contemporary design", "现代建筑", "户外", ["建筑", "现代"]),
    
    # ===== 人像/生活 =====
    ("p-040_photographer.jpg", "Photographer holding camera taking photo outdoors, sunset light, creative person, casual clothing, golden hour", "摄影师拍照", "户外", ["人物", "摄影", "日落"]),
    ("p-041_reading.jpg", "Young woman reading a book by window, cozy indoor, soft natural light, peaceful quiet moment, warm tones", "窗边读书", "室内", ["人物", "阅读", "休闲"]),
    ("p-042_cycling.jpg", "Person cycling on a country road through green forest, sunny day, healthy active lifestyle, motion blur on wheels", "骑行", "户外", ["人物", "运动", "自行车"]),
    ("p-043_yoga.jpg", "Woman doing yoga pose on a mat in bright room with plants, morning light, peaceful wellness lifestyle", "瑜伽", "室内", ["人物", "运动", "瑜伽"]),
    ("p-044_cooking.jpg", "Person cooking in modern kitchen, chopping vegetables, steam from pan, warm home atmosphere", "做饭", "室内", ["人物", "厨房", "烹饪"]),
    
    # ===== 室内/物品 =====
    ("p-045_vinyl_record.jpg", "Vintage vinyl record player with record spinning, retro style, warm moody lighting, music nostalgia", "黑胶唱片机", "室内", ["物品", "音乐", "复古"]),
    ("p-046_camera.jpg", "Vintage film camera on wooden table, leather case, photography equipment, soft light, retro aesthetic", "老式相机", "室内", ["物品", "相机", "复古"]),
    ("p-047_plants.jpg", "Collection of indoor potted plants succulents cacti on white shelf, urban jungle decor, bright natural light", "室内绿植", "室内", ["植物", "家居"]),
    ("p-048_camping.jpg", "Camping tent in forest clearing at night, campfire burning, warm light from tent, stars visible in sky, cozy outdoor adventure", "露营帐篷", "户外", ["露营", "帐篷", "夜景"]),
    
    # ===== 天气/季节 =====
    ("p-049_rain_window.jpg", "Rain drops on window glass, blurry city street outside, cozy moody rainy day atmosphere, gray tones, melancholy feeling", "雨天窗户", "室内", ["雨", "窗户", "天气"]),
    ("p-050_snow_christmas.jpg", "Cozy Christmas tree with lights and ornaments in living room, snow falling outside window, warm festive atmosphere", "圣诞节圣诞树", "室内", ["圣诞", "节日", "雪景"]),
    ("p-051_autumn_leaves.jpg", "Vibrant red orange maple leaves in autumn forest, close up, fall colors, soft sunlight through leaves", "秋天枫叶", "户外", ["秋天", "枫叶", "风景"]),
    ("p-052_morning_coffee.jpg", "Steaming cup of coffee on window sill at morning, sunrise light through window, peaceful morning routine, cozy start of day", "晨间咖啡", "室内", ["咖啡", "早晨", "日常"]),
    
    # ===== 特殊场景 =====
    ("p-053_starry_night.jpg", "Starry night sky over mountain lake, milky way galaxy visible, clear dark sky, astrophotography, reflection in still water", "星空银河", "户外", ["星空", "夜景", "银河"]),
    ("p-054_foggy_forest.jpg", "Mysterious foggy forest in early morning, sunbeams through mist and trees, atmospheric ethereal mood", "晨雾森林", "户外", ["雾", "森林", "风景"]),
    ("p-055_fireworks.jpg", "Spectacular fireworks exploding in night sky over city, colorful bursts, celebration, light trails, New Year atmosphere", "烟花", "户外", ["烟花", "夜景", "节日"]),
    ("p-056_workspace.jpg", "Programmer developer workspace with dual monitors, code on screen, mechanical keyboard, messy desk with coffee, dark room with screen glow", "程序员工作区", "室内", ["办公", "电脑", "编程"]),
]

def gen_one(photo_tuple):
    """生成单张图片（线程函数）"""
    filename, prompt, desc, scene, tags = photo_tuple
    save_path = OUTPUT_DIR / filename
    
    if save_path.exists():
        with print_lock:
            print(f"⏭️  SKIP {filename} (already exists)", flush=True)
        progress["done"] += 1
        return True
    
    url = f"{BASE_URL}/images/generations"
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    
    # 根据场景选尺寸
    size = "1024x1024"
    if scene == "户外" and any(k in " ".join(tags) for k in ["风景", "山", "海", "沙漠", "瀑布", "花田", "极光", "星空"]):
        size = "1536x1024"
    
    payload = {
        "model": "gpt-image-2",
        "prompt": prompt + ", photorealistic, high quality, natural lighting",
        "n": 1,
        "size": size,
        "quality": "medium",
        "response_format": "b64_json",
    }

    for attempt in range(3):
        try:
            if attempt > 0:
                time.sleep(3 * attempt)
            
            with print_lock:
                print(f"🎨 [{progress['done']+1}/{progress['total']}] Generating: {filename} - {desc} (attempt {attempt+1})", flush=True)
            
            r = requests.post(url, headers=headers, json=payload, timeout=180)
            
            if r.status_code == 200:
                data = r.json()
                img_bytes = base64.b64decode(data["data"][0]["b64_json"])
                with open(save_path, "wb") as f:
                    f.write(img_bytes)
                
                with print_lock:
                    print(f"   ✅ {filename} OK! ({len(img_bytes)//1024}KB)", flush=True)
                    progress["success"] += 1
                    progress["done"] += 1
                return True
            else:
                with print_lock:
                    print(f"   ❌ HTTP {r.status_code} for {filename}", flush=True)
                    
        except Exception as e:
            with print_lock:
                print(f"   ❌ Error on {filename}: {type(e).__name__} (attempt {attempt+1})", flush=True)
    
    with print_lock:
        print(f"   💀 FAILED after 3 attempts: {filename}", flush=True)
        progress["done"] += 1
    return False


def main():
    progress["total"] = len(EXTRA_PHOTOS)
    
    print("=" * 70)
    print(f"Photo-Agent 测试集扩充 - {NUM_THREADS}线程并发")
    print("=" * 70)
    print(f"输出目录: {OUTPUT_DIR}")
    print(f"新增图片: {len(EXTRA_PHOTOS)} 张")
    print(f"并发线程: {NUM_THREADS}")
    print()
    
    start_time = time.time()
    
    with ThreadPoolExecutor(max_workers=NUM_THREADS) as executor:
        futures = [executor.submit(gen_one, photo) for photo in EXTRA_PHOTOS]
        for future in as_completed(futures):
            future.result()
    
    elapsed = time.time() - start_time
    
    print("\n" + "=" * 70)
    print(f"生成完成！成功: {progress['success']}/{progress['total']}")
    print(f"耗时: {elapsed:.1f} 秒 (约 {elapsed/60:.1f} 分钟)")
    print(f"平均速度: {elapsed/progress['total']:.1f} 秒/张 (并发加速)")
    print("=" * 70)
    
    # 统计所有图片
    all_images = list(OUTPUT_DIR.glob("p-*.jpg"))
    print(f"\n📁 test_photos 目录中现有 {len(all_images)} 张图片")
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
