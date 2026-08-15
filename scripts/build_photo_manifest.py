"""从本地测试图片生成经过人工复核的 Photo Agent 评测清单。

旧 metadata.json 只用于补充描述和生成来源。会影响评分的场景、物体、人数和
OCR 文本均来自本文件中的人工复核标注，避免把生成提示词误当成图片事实。
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image


# 2026-08-13 对 112 张成图逐张目视复核。这里只放“画面中明确可见”的核心物体；
# 宽泛的主题词放到 search_terms，不参与严格的物体召回评分。
OBJECTS: dict[str, list[str]] = {
    "p-004": ["拉面", "碗", "鸡蛋"],
    "p-006": ["樱花", "树"],
    "p-007": ["生日蛋糕", "蜡烛"],
    "p-008": ["雪山", "草地", "山路"],
    "p-010": ["狗", "草地"],
    "p-011": ["人物", "全家福"],
    "p-012": ["舞台", "乐队", "人群"],
    "p-013": ["飞机", "行李箱", "窗户"],
    "p-014": ["银杏树", "落叶", "道路"],
    "p-015": ["笔记本电脑", "书桌", "杯子"],
    "p-019": ["花园", "鲜花", "小路"],
    "p-021": ["狗", "草地"],
    "p-026": ["寿司", "餐盘"],
    "p-027": ["草莓蛋糕", "茶杯"],
    "p-028": ["可颂", "面包"],
    "p-029": ["汉堡"],
    "p-030": ["饺子", "蒸笼"],
    "p-031": ["极光", "山", "湖"],
    "p-032": ["瀑布", "森林", "水潭"],
    "p-033": ["沙漠", "沙丘", "日落"],
    "p-034": ["薰衣草", "花田"],
    "p-035": ["郁金香", "风车", "花田"],
    "p-036": ["长城", "山"],
    "p-037": ["东方明珠", "建筑", "黄浦江"],
    "p-038": ["石桥", "河道", "古建筑"],
    "p-039": ["现代建筑", "玻璃幕墙"],
    "p-040": ["人物", "相机", "日落"],
    "p-041": ["人物", "书", "窗户"],
    "p-042": ["人物", "自行车", "道路"],
    "p-043": ["人物", "瑜伽垫", "植物"],
    "p-044": ["人物", "蔬菜", "厨房"],
    "p-045": ["唱片机", "黑胶唱片"],
    "p-046": ["相机", "胶卷"],
    "p-047": ["盆栽", "多肉植物", "仙人掌"],
    "p-051": ["枫叶", "树"],
    "p-053": ["银河", "星空", "山", "湖"],
    "p-055": ["烟花", "城市建筑"],
    "p-056": ["电脑显示器", "键盘", "代码"],
    "p-058": ["人物", "咖啡壶", "杯子"],
    "p-060": ["车窗", "雨滴", "汽车"],
    "p-061": ["人物", "狗", "道路"],
    "p-064": ["人物", "衣服", "货架"],
    "p-066": ["外卖盒", "面条", "电视"],
    "p-067": ["人物", "烧烤架", "肉串"],
    "p-068": ["爆米花", "电视", "沙发"],
    "p-069": ["人物", "洗衣机", "衣物"],
    "p-070": ["阳台", "盆栽", "浇水壶"],
    "p-071": ["猫", "笔记本电脑"],
    "p-072": ["人物", "哑铃", "健身器材"],
    "p-073": ["人物", "公园", "道路"],
    "p-074": ["人物", "书", "书桌"],
    "p-075": ["人物", "笔记本电脑", "视频会议"],
    "p-076": ["手", "笔记本", "钢笔"],
    "p-077": ["电影屏幕", "爆米花", "观众"],
    "p-078": ["人物", "便利店", "货架"],
    "p-079": ["人物", "雨伞", "街道"],
    "p-080": ["儿童", "画纸", "蜡笔"],
    "p-081": ["人物", "书", "床"],
    "p-082": ["人物", "餐桌", "酒杯"],
    "p-083": ["毕业生", "学士服", "学士帽"],
    "p-084": ["新娘", "新郎", "婚礼合影"],
    "p-085": ["人物", "会议桌", "白板"],
    "p-086": ["人物", "生日蛋糕", "蜡烛"],
    "p-087": ["学生", "教师", "校服"],
    "p-088": ["人物", "登山包", "雪山"],
    "p-089": ["人群", "舞台"],
    "p-090": ["人物", "圆桌", "菜肴", "灯笼"],
    "p-091": ["人物", "新娘", "香槟杯"],
    "p-092": ["足球队员", "奖杯", "球衣"],
    "p-093": ["人物", "床", "零食"],
    "p-094": ["猫", "玻璃杯", "水"],
    "p-095": ["中餐", "米饭", "餐桌"],
    "p-096": ["狗", "沙发"],
    "p-097": ["窗户", "雨滴", "建筑"],
    "p-098": ["鞋", "湖", "山"],
    "p-099": ["办公桌", "键盘", "文件", "杯子"],
    "p-100": ["汽车", "公路", "车流"],
    "p-101": ["日落", "天空", "车窗"],
    "p-102": ["手", "咖啡杯", "植物"],
    "p-103": ["路牌", "恐龙图案"],
    "p-104": ["汽车", "罚单", "挡风玻璃"],
    "p-105": ["人物", "运动鞋"],
    "p-106": ["人物", "冰箱", "手机"],
    "p-107": ["人物", "手机", "镜子"],
    "p-108": ["快递箱", "门"],
    "p-109": ["停车标志", "道路"],
    "p-110": ["禁止吸烟标志"],
    "p-111": ["路牌", "建筑"],
    "p-112": ["安全出口标志", "门"],
    "p-113": ["咖啡杯"],
    "p-114": ["菜单", "菜品图片"],
    "p-115": ["可乐罐"],
    "p-116": ["麦当劳门店", "招牌"],
    "p-117": ["牛奶盒"],
    "p-118": ["泡面杯", "筷子"],
    "p-119": ["矿泉水瓶"],
    "p-120": ["巧克力", "包装纸"],
    "p-121": ["手机", "锁屏"],
    "p-122": ["笔记本电脑", "代码编辑器"],
    "p-123": ["电视遥控器"],
    "p-124": ["书"],
    "p-125": ["报纸"],
    "p-126": ["笔记本", "手写文字"],
    "p-127": ["火车票"],
    "p-129": ["登机牌"],
    "p-132": ["便利店", "招牌"],
    "p-133": ["台历"],
    "p-134": ["人物", "T恤"],
    "p-135": ["马克杯"],
    "p-136": ["冰箱", "冰箱贴", "便签"],
    "p-137": ["价格标签", "衣服"],
    "p-138": ["门垫", "门"],
}

SCENE_OVERRIDES = {
    "p-013": ["室内", "机场"],
    "p-026": ["餐厅", "室内"],
    "p-027": ["室内", "餐厅"],
    "p-028": ["餐厅", "室内"],
    "p-060": ["车内", "街道"],
    "p-029": ["餐厅", "室内"],
    "p-030": ["餐厅", "室内"],
    "p-070": ["室内", "阳台"],
    "p-078": ["室内", "便利店"],
    "p-084": ["户外", "婚礼"],
    "p-090": ["餐厅", "室内"],
    "p-103": ["户外", "街道"],
    "p-104": ["户外", "街道"],
    "p-116": ["户外", "街道"],
    "p-108": ["户外", "门廊"],
    "p-132": ["街道", "户外"],
    "p-134": ["户外", "街道"],
}

# 精确人数只用于清晰、人数较少的画面；遮挡明显或大群像使用闭区间。
PERSON_RANGES: dict[str, tuple[int, int]] = {
    "p-011": (4, 4),
    "p-012": (10, 1000),
    "p-040": (1, 1),
    "p-041": (1, 1),
    "p-042": (1, 1),
    "p-043": (1, 1),
    "p-044": (1, 1),
    "p-038": (8, 20),
    "p-058": (1, 1),
    "p-060": (1, 1),
    "p-061": (1, 1),
    "p-064": (1, 1),
    "p-067": (5, 5),
    "p-068": (0, 0),
    "p-069": (1, 1),
    "p-072": (2, 4),
    "p-073": (1, 1),
    "p-074": (1, 1),
    "p-075": (1, 1),
    "p-076": (1, 1),
    "p-077": (2, 10),
    "p-078": (3, 3),
    "p-079": (1, 1),
    "p-080": (1, 1),
    "p-081": (1, 1),
    "p-082": (6, 6),
    "p-083": (8, 8),
    "p-084": (15, 20),
    "p-085": (6, 6),
    "p-086": (6, 6),
    "p-087": (22, 22),
    "p-088": (4, 4),
    "p-089": (50, 10000),
    "p-090": (11, 11),
    "p-091": (6, 6),
    "p-092": (13, 13),
    "p-093": (4, 4),
    "p-098": (1, 1),
    "p-102": (1, 1),
    "p-103": (1, 1),
    "p-105": (1, 1),
    "p-106": (1, 1),
    "p-107": (1, 1),
    "p-134": (1, 1),
}

# required_text 是清晰且应识别的评分项；optional_text 只做诊断，不计漏检。
TEXT_LABELS: dict[str, dict[str, list[str]]] = {
    "p-007": {"required": ["Happy Birthday"], "optional": ["Let's Celebrate"]},
    "p-012": {"required": ["LIVE LOUD"], "optional": []},
    "p-028": {"required": [], "optional": ["Croissant"]},
    "p-046": {"required": [], "optional": ["PENTAX"]},
    "p-087": {"required": ["CLASS VI-A", "2002-2003"], "optional": ["DPS"]},
    "p-090": {"required": [], "optional": ["福", "万事如意", "新年快乐"]},
    "p-103": {
        "required": ["PLEASE DRIVE CAREFULLY", "DINOSAURS CROSSING"],
        "optional": [],
    },
    "p-104": {"required": ["PARKING VIOLATION"], "optional": []},
    "p-109": {"required": ["STOP"], "optional": []},
    "p-110": {"required": ["NO SMOKING"], "optional": []},
    "p-111": {"required": ["中山路", "ZHONGSHAN LU"], "optional": []},
    "p-112": {"required": ["EXIT"], "optional": []},
    "p-113": {"required": ["Starbucks"], "optional": ["Rewards"]},
    "p-114": {
        "required": ["宫保鸡丁", "麻婆豆腐", "米饭"],
        "optional": ["热菜", "凉菜", "汤类", "主食"],
    },
    "p-115": {"required": ["Coca-Cola"], "optional": ["140 CALORIES"]},
    "p-116": {"required": ["McDonald's"], "optional": []},
    "p-117": {"required": ["伊利", "纯牛奶"], "optional": ["3.2g", "1L"]},
    "p-118": {"required": ["CUP NOODLES"], "optional": ["BEEF FLAVOUR"]},
    "p-119": {"required": ["农夫山泉"], "optional": ["NONGFU SPRING"]},
    "p-120": {"required": ["HERSHEY'S"], "optional": ["milk chocolate"]},
    "p-121": {"required": ["9:41", "Monday, June 6"], "optional": []},
    "p-122": {"required": ["hello world"], "optional": ["Python"]},
    "p-123": {"required": ["POWER", "VOL", "CH"], "optional": []},
    "p-124": {"required": ["活着", "余华"], "optional": []},
    "p-125": {"required": ["人民日报"], "optional": ["RENMIN RIBAO"]},
    "p-126": {
        "required": ["会议纪要", "待办事项"],
        "optional": ["2024年6月13日", "Q2", "Q3"],
    },
    "p-127": {
        "required": ["北京南", "上海虹桥", "G1"],
        "optional": ["2023年07月01日", "08:00"],
    },
    "p-129": {
        "required": ["BOARDING PASS", "DL 1234", "3A"],
        "optional": ["NEW YORK-JFK", "LOS ANGELES-LAX"],
    },
    "p-132": {"required": ["7-ELEVEN", "OPEN 24 HOURS"], "optional": []},
    "p-133": {"required": ["八月 2026", "15", "星期六"], "optional": []},
    "p-134": {"required": ["I LOVE NY"], "optional": ["I ❤ NY"]},
    "p-135": {"required": ["WORLD'S BEST BOSS"], "optional": []},
    "p-136": {"required": ["买牛奶"], "optional": []},
    "p-137": {"required": ["¥199"], "optional": []},
    "p-138": {"required": ["WELCOME"], "optional": []},
}

CATEGORY_OVERRIDES = {
    photo_id: "OCR文字" for photo_id in TEXT_LABELS if int(photo_id[2:]) >= 109
}


# 2026-08-15 对 test_photos_realistic 的 25 张成图逐张目视复核。
# 这批图均带“AI生成”水印，只能作为“拟真合成困难集”，不能代表真实手机照片。
# p-162/p-163 来自同一近似连拍组，必须固定在同一切分，避免信息泄漏。
REALISTIC_SPLITS = {
    **{
        photo_id: "development"
        for photo_id in (
            "p-139",
            "p-141",
            "p-143",
            "p-146",
            "p-147",
            "p-148",
            "p-150",
            "p-152",
            "p-153",
            "p-155",
            "p-158",
            "p-159",
            "p-160",
            "p-162",
            "p-163",
        )
    },
    **{
        photo_id: "validation"
        for photo_id in ("p-140", "p-144", "p-154", "p-156", "p-161")
    },
    **{photo_id: "test" for photo_id in ("p-142", "p-145", "p-149", "p-151", "p-157")},
}

REALISTIC_GROUND_TRUTH: dict[str, dict[str, Any]] = {
    "p-139": {
        "scenes": ["餐厅", "室内"],
        "required": ["食物", "盘子"],
        "optional": ["蜡烛", "餐桌"],
        "persons": (0, 0),
        "required_text": [],
        "optional_text": [],
        "summary": "暗光餐厅中，桌上放着一盘食物，旁边有蜡烛照明",
    },
    "p-140": {
        "scenes": ["室内", "餐厅"],
        "required": ["酒瓶", "吧台"],
        "optional": ["玻璃窗", "人物剪影"],
        "persons": (0, 3),
        "required_text": [],
        "optional_text": [],
        "summary": "昏暗酒吧吧台后方陈列着一排酒瓶，玻璃上有反光",
    },
    "p-141": {
        "scenes": ["卧室", "室内"],
        "required": ["手机", "闹钟"],
        "optional": ["水杯", "床头柜"],
        "persons": (0, 0),
        "required_text": [],
        "optional_text": [],
        "summary": "深夜床头柜上亮着手机屏幕，旁边有闹钟和水杯",
    },
    "p-142": {
        "scenes": ["室内"],
        "required": ["停车场", "汽车"],
        "optional": ["车位", "顶灯"],
        "persons": (0, 0),
        "required_text": [],
        "optional_text": [],
        "summary": "昏暗地下停车场通道两侧停放着多辆汽车",
    },
    "p-143": {
        "scenes": ["户外"],
        "required": ["狗", "草地"],
        "optional": [],
        "persons": (0, 0),
        "required_text": [],
        "optional_text": [],
        "summary": "一只狗在绿色草地上快速奔跑，主体有明显运动模糊",
    },
    "p-144": {
        "scenes": ["车内", "街道"],
        "required": ["车窗", "汽车"],
        "optional": ["建筑", "街道"],
        "persons": (0, 0),
        "required_text": [],
        "optional_text": [],
        "summary": "从公交车窗拍摄城市道路，窗外车辆和建筑明显模糊",
    },
    "p-145": {
        "scenes": ["客厅", "室内"],
        "required": ["人物", "沙发"],
        "optional": ["儿童", "电视"],
        "persons": (1, 1),
        "required_text": [],
        "optional_text": [],
        "summary": "客厅里一名儿童从沙发前跑过，人物因运动而模糊",
    },
    "p-146": {
        "scenes": ["卧室", "室内"],
        "required": ["衣服", "床"],
        "optional": ["枕头"],
        "persons": (0, 0),
        "required_text": [],
        "optional_text": [],
        "summary": "床上杂乱堆放着大量未整理的衣服",
    },
    "p-147": {
        "scenes": ["室内"],
        "required": ["数据线", "插头"],
        "optional": ["充电器"],
        "persons": (0, 0),
        "required_text": [],
        "optional_text": [],
        "summary": "地面上堆着相互缠绕的数据线、插头和充电器",
    },
    "p-148": {
        "scenes": ["商店", "室内"],
        "required": ["货架", "商品"],
        "optional": ["超市通道"],
        "persons": (0, 0),
        "required_text": [],
        "optional_text": [],
        "summary": "超市通道两侧是摆满商品的高大货架",
    },
    "p-149": {
        "scenes": ["餐厅", "室内"],
        "required": ["餐桌", "盘子"],
        "optional": ["餐具", "纸巾", "剩菜"],
        "persons": (0, 0),
        "required_text": [],
        "optional_text": [],
        "summary": "饭后餐桌上散放着空盘、餐具、纸巾和剩菜",
    },
    "p-150": {
        "scenes": ["户外"],
        "required": ["雪地", "树"],
        "optional": ["雪景"],
        "persons": (0, 0),
        "required_text": [],
        "optional_text": [],
        "summary": "严重过曝的雪地景色，只能隐约看到远处树木",
    },
    "p-151": {
        "scenes": ["室内"],
        "required": ["人物", "窗户"],
        "optional": ["剪影"],
        "persons": (1, 1),
        "required_text": [],
        "optional_text": [],
        "summary": "一名人物背对镜头站在明亮窗前，逆光形成黑色剪影",
    },
    "p-152": {
        "scenes": ["室内"],
        "required": ["玩具熊", "桌子"],
        "optional": ["影子"],
        "persons": (0, 0),
        "required_text": [],
        "optional_text": [],
        "summary": "闪光灯照亮桌上的玩具熊，并在墙上投下明显硬阴影",
    },
    "p-153": {
        "scenes": ["街道", "户外"],
        "required": ["路牌"],
        "optional": ["天空"],
        "persons": (0, 0),
        "required_text": ["西四环南大街"],
        "optional_text": [],
        "summary": "倾斜构图拍摄写有“西四环南大街”的蓝色路牌",
    },
    "p-154": {
        "scenes": ["室内"],
        "required": ["天花板", "日光灯"],
        "optional": [],
        "persons": (0, 0),
        "required_text": [],
        "optional_text": [],
        "summary": "误拍的室内天花板和成排日光灯，画面明显倾斜",
    },
    "p-155": {
        "scenes": ["室内", "户外"],
        "required": ["窗户", "建筑"],
        "optional": ["反光", "树"],
        "persons": (0, 0),
        "required_text": [],
        "optional_text": [],
        "summary": "隔着窗户拍摄住宅楼和树木，玻璃反光覆盖部分画面",
    },
    "p-156": {
        "scenes": ["室内"],
        "required": ["鱼", "水族箱"],
        "optional": ["玻璃", "水草"],
        "persons": (0, 0),
        "required_text": [],
        "optional_text": [],
        "summary": "隔着反光且带污渍的水族箱玻璃拍摄多条鱼",
    },
    "p-157": {
        "scenes": ["车内", "街道"],
        "required": ["挡风玻璃", "汽车"],
        "optional": ["雨滴", "雨刷", "公路"],
        "persons": (0, 0),
        "required_text": [],
        "optional_text": [],
        "summary": "雨天从车内透过有雨水的挡风玻璃拍摄前方公路和汽车",
    },
    "p-158": {
        "scenes": ["街道", "户外"],
        "required": ["街道", "汽车"],
        "optional": ["路灯", "树"],
        "persons": (0, 0),
        "required_text": [],
        "optional_text": [],
        "summary": "镜头起雾后拍摄的街道，车辆、路灯和树木都很朦胧",
    },
    "p-159": {
        "scenes": ["餐厅", "室内"],
        "required": ["食物", "盘子"],
        "optional": ["餐桌", "油污"],
        "persons": (0, 0),
        "required_text": [],
        "optional_text": [],
        "summary": "透过带油污的镜头拍摄盘中食物，画面有明显彩色眩光",
    },
    "p-160": {
        "scenes": ["其他"],
        "required": ["天气应用", "天气图标"],
        "optional": ["手机界面"],
        "persons": (0, 0),
        "required_text": ["WEATHER"],
        "optional_text": ["Today", "Tomorrow"],
        "summary": "天气应用截图，列出一周天气、温度和天气图标",
    },
    "p-161": {
        "scenes": ["其他"],
        "required": ["地图", "导航路线"],
        "optional": ["手机界面", "定位标记"],
        "persons": (0, 0),
        "required_text": ["MAPS"],
        "optional_text": ["MAIN ST", "DISTANCE: 2.4 mi", "ETA: 8 MINUTES"],
        "summary": "地图导航应用截图，蓝色路线从当前位置通向主街",
    },
    "p-162": {
        "scenes": ["室内"],
        "required": ["猫", "窗户"],
        "optional": ["盆栽", "窗台"],
        "persons": (0, 0),
        "required_text": [],
        "optional_text": [],
        "summary": "橘猫坐在窗台上面向窗外，旁边放着盆栽",
    },
    "p-163": {
        "scenes": ["室内"],
        "required": ["猫", "窗户"],
        "optional": ["窗台"],
        "persons": (0, 0),
        "required_text": [],
        "optional_text": [],
        "summary": "橘猫坐在窗台上侧身望向窗外",
    },
}


def _read_legacy(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {item["id"]: item for item in payload.get("photos", [])}


def _script_records(scripts_dir: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for script in scripts_dir.glob("generate_*.py"):
        tree = ast.parse(script.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            names = {
                target.id for target in node.targets if isinstance(target, ast.Name)
            }
            if not names.intersection(
                {"TEXT_PHOTOS", "EXTRA_PHOTOS", "DAILY_PHOTOS", "PHOTOS"}
            ):
                continue
            values = ast.literal_eval(node.value)
            for value in values:
                if not isinstance(value, tuple) or not str(value[0]).startswith("p-"):
                    continue
                filename, prompt, description = value[:3]
                photo_id = filename.split("_", 1)[0]
                if "TEXT_PHOTOS" in names:
                    scene, expected_text, objects, persons = value[3:7]
                    tags = list(objects)
                elif len(value) == 6:
                    _, scene, tags = value[3:6]
                    persons = None
                    expected_text = []
                else:
                    scene, tags = value[3:5]
                    persons = None
                    expected_text = []
                records[photo_id] = {
                    "filename": filename,
                    "prompt": prompt,
                    "description": description,
                    "scene": scene,
                    "tags": list(tags),
                    "prompt_expected_text": list(expected_text),
                    "prompt_persons": persons,
                    "generator": script.name,
                }
    return records


def _split(index: int) -> str:
    remainder = index % 5
    return (
        "test" if remainder == 0 else "validation" if remainder == 1 else "development"
    )


def _realistic_records(
    images_dir: Path,
    metadata_path: Path,
) -> list[dict[str, Any]]:
    metadata = _read_legacy(metadata_path)
    images = sorted(images_dir.glob("p-*.jpg"))
    records: list[dict[str, Any]] = []
    if not images:
        raise ValueError(f"拟真合成困难集为空: {images_dir}")
    for image_path in images:
        photo_id = image_path.name.split("_", 1)[0]
        if photo_id not in REALISTIC_GROUND_TRUTH:
            raise ValueError(f"拟真合成图片尚未人工复核: {photo_id}")
        if photo_id not in REALISTIC_SPLITS:
            raise ValueError(f"拟真合成图片未冻结切分: {photo_id}")
        label = REALISTIC_GROUND_TRUTH[photo_id]
        base = metadata.get(photo_id, {})
        with Image.open(image_path) as image:
            width, height = image.size
        record: dict[str, Any] = {
            "id": photo_id,
            "filename": image_path.name,
            "path": f"test_photos_realistic/{image_path.name}",
            "sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
            "width": width,
            "height": height,
            "source": "synthetic",
            "dataset_role": "synthetic_robustness",
            "simulates_phone_photo": True,
            "split": REALISTIC_SPLITS[photo_id],
            "category": str(base.get("category") or "拟真困难样本"),
            "challenge_type": str(base.get("challenge_type") or "unknown"),
            "generation": {
                "generator": "test_photos_realistic/metadata.json",
                "prompt": None,
            },
            "ground_truth": {
                "acceptable_scenes": label["scenes"],
                "required_objects": label["required"],
                "optional_objects": label["optional"],
                "persons": {
                    "min": label["persons"][0],
                    "max": label["persons"][1],
                },
                "required_text": label["required_text"],
                "optional_text": label["optional_text"],
                "ignored_text": ["AI生成"],
                "summary": label["summary"],
                "search_terms": list(
                    dict.fromkeys([*base.get("tags", []), *label["required"]])
                ),
            },
            "review": {
                "status": "human_reviewed",
                "reviewed_at": "2026-08-15",
                "notes": (
                    "逐图目视复核；图片均为带‘AI生成’水印的拟真合成图，"
                    "只用于低光、模糊、反光、过曝、杂乱、截屏和近似重复等鲁棒性评测；"
                    "水印不参与 OCR 评分，也不得将本批结果表述为真实手机照片效果。"
                ),
            },
        }
        if base.get("burst_group"):
            record["group_id"] = str(base["burst_group"])
            record["group_index"] = int(base.get("burst_index") or 0)
        records.append(record)
    if set(REALISTIC_GROUND_TRUTH) != {record["id"] for record in records}:
        missing = sorted(
            set(REALISTIC_GROUND_TRUTH) - {record["id"] for record in records}
        )
        raise ValueError(f"拟真合成标注缺少对应图片: {missing}")
    return records


def build_manifest(
    images_dir: Path,
    legacy_path: Path,
    scripts_dir: Path,
    realistic_images_dir: Path = Path("test_photos_realistic"),
    realistic_metadata_path: Path = Path("test_photos_realistic/metadata.json"),
) -> dict[str, Any]:
    legacy = _read_legacy(legacy_path)
    scripted = _script_records(scripts_dir)
    images = sorted(images_dir.glob("p-*.jpg"))
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, image_path in enumerate(images):
        photo_id = image_path.name.split("_", 1)[0]
        if photo_id in seen_ids:
            raise ValueError(f"重复图片 ID: {photo_id}")
        seen_ids.add(photo_id)
        if photo_id not in OBJECTS:
            raise ValueError(f"图片尚未人工复核: {photo_id}")
        base = {**legacy.get(photo_id, {}), **scripted.get(photo_id, {})}
        description = str(base.get("description") or image_path.stem.replace("_", " "))
        default_scene = str(base.get("scene") or "其他")
        scenes = SCENE_OVERRIDES.get(photo_id, [default_scene])
        person_min, person_max = PERSON_RANGES.get(photo_id, (0, 0))
        text = TEXT_LABELS.get(photo_id, {"required": [], "optional": []})
        tags = list(dict.fromkeys([*base.get("tags", []), *OBJECTS[photo_id]]))
        with Image.open(image_path) as image:
            width, height = image.size
        records.append(
            {
                "id": photo_id,
                "filename": image_path.name,
                "path": f"test_photos/{image_path.name}",
                "sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
                "width": width,
                "height": height,
                "source": "synthetic",
                "split": _split(index),
                "category": CATEGORY_OVERRIDES.get(
                    photo_id, base.get("category", "其他")
                ),
                "generation": {
                    "generator": base.get("generator"),
                    "prompt": base.get("prompt"),
                },
                "ground_truth": {
                    "acceptable_scenes": scenes,
                    "required_objects": OBJECTS[photo_id],
                    "optional_objects": [],
                    "persons": {"min": person_min, "max": person_max},
                    "required_text": text["required"],
                    "optional_text": text["optional"],
                    "summary": description,
                    "search_terms": tags,
                },
                "review": {
                    "status": "human_reviewed",
                    "reviewed_at": "2026-08-13",
                    "notes": (
                        "人数按最终成图复核；只统计主体场景中清晰可辨的人，"
                        "排除屏幕/海报/照片、孤立肢体和很小的模糊背景人物；"
                        "大群像使用区间；可选文字漏识别不扣分。"
                    ),
                },
            }
        )
    records.extend(_realistic_records(realistic_images_dir, realistic_metadata_path))
    if len(records) != 137:
        raise ValueError(f"期望 137 张图片，实际 {len(records)} 张")
    return {
        "version": "2.1.0",
        "name": "Photo Agent 人工复核图片评测集（含拟真合成困难集）",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "reviewed_at": "2026-08-15",
        "image_root": "../..",
        "total_images": len(records),
        "annotation_policy": {
            "scene": "同时报告精确命中与室内/户外层级兼容命中",
            "objects": "required_objects 参与召回；额外合理物体不作为误检",
            "persons": (
                "必须落在 min/max 闭区间；只统计主体场景中清晰可辨的人，"
                "排除屏幕/海报/照片、孤立肢体和很小的模糊背景人物"
            ),
            "ocr": "required_text 参与召回；optional_text 只做诊断",
            "synthetic_robustness": (
                "p-139 至 p-163 均为带水印的拟真合成图；水印属于 ignored_text，"
                "该切片必须单独报告，不能作为真实手机照片结论"
            ),
            "group_split": "同一 group_id 的近似重复图片必须位于同一切分",
        },
        "images": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="构建人工复核图片评测清单")
    parser.add_argument("--images-dir", default="test_photos")
    parser.add_argument("--legacy-metadata", default="test_photos/metadata.json")
    parser.add_argument("--scripts-dir", default="gen_photo")
    parser.add_argument("--realistic-images-dir", default="test_photos_realistic")
    parser.add_argument(
        "--realistic-metadata",
        default="test_photos_realistic/metadata.json",
    )
    parser.add_argument("--output", default="tests/eval/photo_manifest.json")
    args = parser.parse_args()
    manifest = build_manifest(
        Path(args.images_dir),
        Path(args.legacy_metadata),
        Path(args.scripts_dir),
        Path(args.realistic_images_dir),
        Path(args.realistic_metadata),
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"已写入 {output}：{manifest['total_images']} 张人工复核图片")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
