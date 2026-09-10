"""Materialize 80 new Chinese queries from pre-outcome visual review.

This is annotation data, never a runtime model prompt. All forty selected images
were inspected before authoring; every explicit condition is visually grounded.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "tests/eval/retrieval_validation"

# query, positive image numbers, explicitly confusable negatives, family, extra tags, OCR
ROWS = [
    ("找蓝眼睛无毛猫趴在浅色织物上的近照", [1], [2], "cats", [], []),
    ("找黑白花猫蜷缩在条纹床单上睡觉的照片", [2], [1, 4], "cats", [], []),
    ("找草地障碍训练场里棕色狗朝镜头跑来的照片", [3], [4], "dogs", ["action"], []),
    ("找黑色狗躺在室内黑色垫子上休息的照片", [4], [2, 3], "dogs", [], []),
    ("找咖啡机正把两股咖啡流入白色杯子的照片", [5], [6], "coffee", ["action"], []),
    ("找俯拍一杯咖啡，中间有大片白色奶泡的照片", [6], [5], "coffee", [], []),
    ("找穿格子衣服的人用手托着打开的书阅读的近照", [7], [8, 28], "books", ["action"], []),
    ("找许多不同颜色的书横着叠成一大摞的照片", [8], [7], "books", [], []),
    ("找几个人在白色酒馆旁的街道上骑自行车的照片", [9], [10], "bicycles", ["action"], []),
    ("找蓝色自行车停在涂鸦门窗前的照片", [10], [9], "bicycles", [], []),
    ("找草地上藤编提篮装满红苹果的照片", [11], [12, 38], "fruit", [], []),
    ("找白色背景上三个完整橘子，其中有带绿叶的橘子", [12], [11], "fruit", [], []),
    ("找一只手托着加了浅色奶油的可颂，背后有红色座椅", [13], [14], "pastry", [], []),
    ("找玻璃柜里分层展示许多不同糖霜甜甜圈的照片", [14], [13], "pastry", [], []),
    ("找有金属雨棚的月台旁停着黄色车头列车的照片", [15], [16], "transit", [], []),
    ("找路边青绿色弧形候车亭，亭子里有一排座椅但没人", [16], [15], "transit", [], []),
    ("找路旁红色八角形牌子写着STOP的照片", [17], [18, 40], "signs", ["ocr"], ["STOP"]),
    ("找写有TURNING VEHICLES、右转箭头和行人图案的路牌", [18], [17, 39], "signs", ["ocr"], ["TURNING VEHICLES"]),
    ("找被草木部分遮住、写SPEED LIMIT 30的白色路牌", [19], [20], "signs", ["ocr"], ["SPEED LIMIT", "30"]),
    ("找白色牌面红色圆圈里写着50的限速标志", [20], [19], "signs", ["ocr"], ["50"]),
    ("找晴朗蓝天下大片黄色向日葵花田的照片", [21], [22], "flowers", [], []),
    ("找黑色背景上一朵红玫瑰的特写", [22], [21], "flowers", [], []),
    ("找岩石间分成多股白色水流的瀑布，周围有绿树", [23], [24, 25], "inland_water", [], []),
    ("找石拱桥横跨小河、两岸有草和树枝的照片", [24], [23], "inland_water", [], []),
    ("找群山和岩石积雪、前景有蓝绿色小湖的照片", [25], [23, 24, 26], "inland_water", [], []),
    ("找阴天下空旷沙滩和灰色海面的照片", [26], [32, 23], "coast", [], []),
    ("找街边戴墨镜、穿黑外套的人弹木吉他的照片", [27], [28], "music", ["action"], []),
    ("找黑白风格的双手弹钢琴近照，手腕上戴着圆形表", [28], [27, 36], "music", ["action"], []),
    ("找有人坐在轮椅上在白色厨房灶台前操作锅具的照片", [29], [30], "kitchen", ["action"], []),
    ("找戴浅色帽子的人用绿色海绵在流水下洗白盘的照片", [30], [29], "kitchen", ["action"], []),
    ("找湿漉漉的街头有人撑蓝色雨伞，周围有水雾和强光灯", [31], [32], "umbrellas", [], []),
    ("找海边几把草编遮阳伞在地上投下圆形影子的照片", [32], [31, 26], "umbrellas", [], []),
    ("找几个人在室外碎石地上打篮球，其中有人穿粉色上衣", [33], [34], "sports", ["action"], []),
    ("找草地足球场上红白两队球员踢球的照片", [34], [33], "sports", ["action"], []),
    ("找打开屏幕、黑屏上有反光的整台笔记本电脑", [35], [36], "computers", [], []),
    ("找黑色电脑键盘按键的近照，按键边缘有明显灰尘", [36], [35, 28], "computers", [], []),
    ("找超市里的绿色有孔购物推车，里面放着蔬菜盒和包装食品", [37], [38, 11], "shopping", [], []),
    ("找红砖墙前摆着几个不同形状藤编提篮的照片", [38], [11, 37], "shopping", [], []),
    ("找有蓝底P、写着Mon-Sat和2 hours的白色停车牌", [39], [40, 19], "signs", ["ocr"], ["Mon-Sat", "2 hours"]),
    ("找木柱上有裂纹的旧牌子，写着NO PARKING ANY TIME", [40], [39, 17], "signs", ["ocr"], ["NO PARKING", "ANY TIME"]),
    ("把猫的照片找出来", [1, 2], [3, 4], "cats", ["multi_positive", "colloquial"], []),
    ("我想看狗的照片", [3, 4], [1, 2], "dogs", ["multi_positive", "colloquial"], []),
    ("找和咖啡有关的照片，制作过程和做好的咖啡都要", [5, 6], [], "coffee", ["multi_positive", "colloquial"], []),
    ("找以书本为主要拍摄对象的照片", [7, 8], [], "books", ["multi_positive"], []),
    ("找拍到自行车的照片，骑着的和停着的都要", [9, 10], [29], "bicycles", ["multi_positive", "colloquial"], []),
    ("找以苹果或橘子为主体的水果照片", [11, 12], [37], "fruit", ["multi_positive"], []),
    ("找可颂面包或者甜甜圈的照片", [13, 14], [37], "pastry", ["multi_positive"], []),
    ("找有公共交通候车雨棚或候车亭的照片", [15, 16], [], "transit", ["multi_positive"], []),
    ("找交通标志或停车标志作为主要拍摄对象的照片", [17, 18, 19, 20, 39, 40], [27], "signs", ["multi_positive"], []),
    ("找花朵作为主要拍摄对象的照片", [21, 22], [], "flowers", ["multi_positive"], []),
    ("找河流、瀑布或者高山湖泊的自然风景照片", [23, 24, 25], [26, 32], "inland_water", ["multi_positive"], []),
    ("找人物正在弹奏乐器的照片", [27, 28], [36], "music", ["multi_positive", "action"], []),
    ("找睡着的猫，不要睡着的狗", [2], [1, 4], "cats", ["exclusion", "colloquial"], []),
    ("我只要眼睛睁着的猫，别给我睡觉的猫", [1], [2], "cats", ["exclusion", "colloquial"], []),
    ("要正在跑的狗，不要躺着的狗", [3], [4], "dogs", ["exclusion", "action"], []),
    ("找自行车停着而且没有人正在骑它的照片", [10], [9], "bicycles", ["exclusion", "action"], []),
    ("咖啡已经做好而且有白色奶泡，不要咖啡机正在出液的", [6], [5], "coffee", ["exclusion", "action"], []),
    ("找在水龙头下洗盘子，不要在灶台上做饭", [30], [29], "kitchen", ["exclusion", "action"], []),
    ("只找电脑键盘特写，不要整台笔记本，也不要钢琴键盘", [36], [35, 28], "computers", ["exclusion"], []),
    ("要海边的草编遮阳伞，不要街头蓝色雨伞", [32], [31, 26], "umbrellas", ["exclusion"], []),
    ("路牌上是STOP，不是YIELD", [17], [18], "signs", ["ocr", "exclusion"], ["STOP"]),
    ("找能看到YIELD、TO文字和行人图案的路牌", [18], [17], "signs", ["ocr"], ["YIELD", "TO"]),
    ("找限速数字30的牌子，不要50", [19], [20], "signs", ["ocr", "exclusion"], ["30"]),
    ("找红圈里面写50的限速牌，不要写30的", [20], [19], "signs", ["ocr", "exclusion"], ["50"]),
    ("找写着2 hours的停车标志", [39], [40], "signs", ["ocr"], ["2 hours"]),
    ("找有NO PARKING ANY TIME英文的路牌", [40], [39], "signs", ["ocr"], ["NO PARKING", "ANY TIME"]),
    ("找写着8 am - 6 pm的停车牌", [39], [40], "signs", ["ocr"], ["8 am", "6 pm"]),
    ("找圆形地面贴纸上写着WAIT HERE的照片", [37], [39, 14], "shopping", ["ocr"], ["WAIT HERE"]),
    ("找戴墨镜的猫", [], [1, 2, 27], "cats", ["zero_result", "object_conflict"], []),
    ("找雪地里奔跑的狗", [], [3, 4, 25], "dogs", ["zero_result", "scene_conflict"], []),
    ("找咖啡奶泡上写着2026的照片", [], [6], "coffee", ["zero_result", "ocr"], ["2026"]),
    ("找有人一边骑自行车一边读书的照片", [], [7, 9], "books", ["zero_result", "action_conflict"], []),
    ("找同一个藤篮里同时装着苹果和橘子的照片", [], [11, 12, 38], "fruit", ["zero_result", "object_conflict"], []),
    ("找蓝色糖霜甜甜圈上写着Happy Birthday的照片", [], [13, 14], "pastry", ["zero_result", "ocr"], ["Happy Birthday"]),
    ("找黄色车头的火车从石拱桥上开过的照片", [], [15, 24], "transit", ["zero_result", "scene_conflict"], []),
    ("找限速数字80的路牌", [], [19, 20], "signs", ["zero_result", "ocr"], ["80"]),
    ("找雪地里盛开的向日葵", [], [21, 25], "flowers", ["zero_result", "scene_conflict"], []),
    ("找有人在户外草地上弹钢琴的照片", [], [27, 28], "music", ["zero_result", "scene_conflict"], []),
    ("找有人做饭时怀里抱着狗的照片", [], [29, 3, 4], "kitchen", ["zero_result", "object_conflict"], []),
    ("找有人在雪地上打篮球的照片", [], [33, 25], "sports", ["zero_result", "scene_conflict"], []),
]


def main():
    target = OUT / "annotations.json"
    data = json.loads(target.read_text(encoding="utf-8"))
    assert len(data["images"]) == 40 and not data["retrieval_outcomes_observed"]
    assert len(ROWS) == 80
    data["queries"] = [{"id": f"val-{i:03d}", "query": text,
                        "relevant_photo_ids": [f"v-{p:03d}" for p in pos],
                        "hard_negative_photo_ids": [f"v-{p:03d}" for p in neg],
                        "family_id": "validation-" + family,
                        "tags": list(dict.fromkeys((["visual_semantic"] if i <= 40 else []) + tags)),
                        "required_visible_text": ocr}
                       for i, (text, pos, neg, family, tags, ocr) in enumerate(ROWS, 1)]
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Authored 80 queries: 40 specific, 12 multi-positive, 16 constraint/OCR, 12 empty")


if __name__ == "__main__":
    main()
