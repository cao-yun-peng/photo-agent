"""Materialize Codex's pre-retrieval visual review; never call the SUT here."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TASK = ROOT / '.project-to-act/tasks/S6-RETRIEVAL-20260908'
OUT = ROOT / 'tests/eval/retrieval_v2'

EDITS = {
    6: ('找棕褐色旧照片中四个人坐在一起的老式全家福', 'p-011 为棕褐色旧照片，不宜要求严格黑白。'),
    34: ('找白色架子上仙人掌、多肉和其他盆栽摆在一起的照片', 'p-047 可见白色架子，但无法确认它是窗台。'),
    40: ('找下雨时从公交车侧窗看到近处银白色汽车的照片', '放大 p-060 后可见车身偏银色，避免颜色硬冲突。'),
    53: ('找手拿黑色笔在横线笔记本上写英文笔记的近照', '放大 p-076 后笔尖不是钢笔，改为可确认的黑色笔。'),
    55: ('找便利店里一个人站在柜台前的照片', 'p-078 室内照明无法证明深夜。'),
    125: ('找一个人站在亮窗前形成黑色剪影的照片', '放大 p-151 仍不能可靠判断人物正背朝向。'),
    128: ('找倾斜构图的室内天花板和成排日光灯照片', 'p-154 可确认倾斜，不能从图像确认误拍意图。'),
    146: ('头发乱糟糟，拿手机照镜子的照片', '移除无法确认的刚醒时间状态。'),
    154: ('相机对着天花板，还拍歪了的照片', '移除无法确认的不小心拍摄意图。'),
}

# Independent observations recorded after inspecting all nine contact sheets.
# Fine OCR/direction ambiguities were inspected at original resolution as noted.
OBSERVATIONS = '''004|黑碗拉面、切半鸡蛋、叉烧
006|粉樱花与公园步道
007|无人巧克力生日蛋糕、点燃蜡烛
008|草地徒步路与远处雪山
010|两只金毛在草地奔跑
011|棕褐色四人旧全家福
012|近景乐队舞台、LIVE LOUD
013|候机厅行李箱、窗外飞机
014|银杏大道黄叶
015|窗边整洁木桌、电脑白杯盆栽
019|石屋花园拱门小路
021|单只小金毛草地坐姿
026|三文鱼寿司与海苔卷
027|白色草莓蛋糕与茶具
028|咖啡馆可颂
029|清晰完整芝士汉堡近景
030|竹蒸笼饺子
031|绿紫极光山湖
032|森林瀑布水潭
033|沙丘日落
034|紫薰衣草条带花田
035|彩色郁金香花田风车
036|绿色山脊长城
037|东方明珠江面夜景
038|水乡石拱桥河道白房
039|棱角蓝玻璃高楼
040|海边夕阳举相机的人
041|窗边穿毛衣女生读书
042|戴头盔骑车的人与绿树道路
043|室内瑜伽女生与绿植
044|厨房女生切菜
045|木柜唱片机与黑胶
046|桌上老式相机胶卷
047|白色架子多肉仙人掌盆栽
051|红橙枫叶阳光
053|银河山湖倒影
055|城市水边烟花
056|双显示器代码工作桌
058|厨房用金属壶往白杯倒咖啡
060|公交侧窗雨滴、银白汽车
061|树荫道路牵金毛的人
064|女生衣架前选衣服、599牌
066|电视前盒装面条、可乐
067|男人用夹子烧烤肉串、身后朋友
068|沙发毯子腿部爆米花看电视
069|女生往滚筒洗衣机放衣服
070|阳台盆栽绿色浇水壶
071|灰猫睡在电脑键盘
072|健身房男人举哑铃
073|黑衣女生公园跑步
074|台灯下男人写作业
075|女生向电脑多人视频会议挥手
076|手持黑笔英文横线笔记
077|影院银幕人物、前景爆米花
078|便利店柜台前黑衣顾客
079|湿街道彩虹伞人物背影
080|小孩蜡笔画房子太阳
081|女生被窝台灯看书
082|朋友餐桌举酒杯自拍
083|毕业合影抛学士帽
084|户外婚礼花拱门亲友合影
085|白板会议室围桌讨论
086|朋友围寿星点蜡烛生日蛋糕
087|校服学生老师分排合影
088|四人登山包、山顶路牌与山景
089|音乐节巨大观众全景、远处小舞台
090|红灯笼圆桌团圆饭
091|新娘与浅色晨袍伴娘准备
092|球衣队员举奖杯
093|女生宿舍裹毯子零食聚会
094|橘猫爪碰倒透明玻璃杯、洒水
095|米饭青菜肉菜餐前桌面
096|金毛沙发仰躺睡觉
097|办公室电脑前雨窗楼房
098|白鞋伸向蓝绿山湖
099|凌乱办公桌纸张便签键盘杯子
100|驾驶位看堵车尾灯与晚霞
101|汽车侧窗看粉橙晚霞、路边拖影
102|手拿浅色咖啡杯、窗边木台植物
103|恐龙三角路牌、DINOSAURS CROSSING
104|雨刷夹黄单、PARKING VIOLATION
105|牛仔裤白鞋俯拍
106|暗厨房人物打开近空冰箱、钟12:17
107|乱发手机镜面自拍、背后床
108|家门口堆品牌快递箱
109|红八角STOP
110|禁烟标志、NO SMOKING
111|蓝路牌中山路ZHONGSHAN LU
112|门上绿灯箱EXIT
113|星巴克外带纸杯与Starbucks文字
114|展开中文菜名价格图片菜单
115|红色Coca-Cola罐近景
116|黄色M与McDonald's门店
117|伊利纯牛奶蓝白盒
118|电脑前CUP NOODLES杯面筷子
119|红盖农夫山泉瓶
120|银色HERSHEY'S包装与巧克力块
121|手机锁屏9:41
122|笔记本代码hello world
123|沙发黑遥控器
124|余华活着书封面
125|人民日报报纸桌面
126|中文手写会议纪要待办事项
127|蓝火车票北京南上海虹桥G1
129|登机牌BOARDING PASS座位3A
132|夜间7-ELEVEN与OPEN 24 HOURS
133|台历八月2026与15
134|白T恤I红心NY
135|白杯WORLD'S BEST BOSS
136|冰箱贴与买牛奶手写便签
137|衣服吊牌199元
138|棕门垫WELCOME
139|黑暗桌面烛光照盘中食物
140|暗酒吧整排酒瓶背光
141|暗床头亮手机闹钟水杯
142|地下车库两侧车辆通道
143|草地狗明显运动模糊
144|公交侧窗拍城市车辆拖影
145|客厅沙发前小孩跑动拖影
146|床面堆满衣服
147|地上插头数据线充电器缠绕
148|超市食品货架长通道
149|剩菜餐具脏纸巾饭后桌
150|近全白过曝雪地淡树影
151|亮窗前人物黑剪影、方向不可确认
152|闪光白玩具熊及大硬阴影
153|倾斜蓝路牌西四南大街、不含环
154|倾斜天花板日光灯
155|反光玻璃外楼房树晴天
156|鱼缸污渍反光及多条鱼
157|前挡雨滴雨刷前方车辆公路
158|起雾玻璃街灯模糊车影
159|指纹油膜彩色光晕遮汉堡薯条
160|WEATHER天气温度列表应用截图
161|MAPS蓝导航路线应用截图
162|背朝镜头窗台橘猫盆栽、窗外鸟
163|侧脸朝镜头侧窗台橘猫'''

def main():
    old = ROOT / 'tests/eval/retrieval'
    corpus = json.loads((old/'corpus.json').read_text(encoding='utf8'))
    queries = [json.loads(l) for l in (old/'queries.jsonl').read_text(encoding='utf8').splitlines()]
    observations = dict(('p-'+line.split('|')[0], line.split('|')[1]) for line in OBSERVATIONS.splitlines())
    assert set(observations) == {x['photo_id'] for x in corpus}
    # Queries sharing relevant targets belong to one connected family. In
    # particular, multi-positive dog/cat/cake queries must not be independent
    # bootstrap units from their corresponding single-image paraphrases.
    parents = {p:p for p in observations}
    def find(p):
        while parents[p] != p:
            parents[p] = parents[parents[p]]
            p = parents[p]
        return p
    for q in queries:
        ids = q['relevant_photo_ids']
        for p in ids[1:]:
            roots = sorted((find(ids[0]),find(p)))
            parents[roots[1]] = roots[0]
    changes = []
    reviews = []
    for q in queries:
        n = int(q['id'].split('-')[1])
        if n in EDITS:
            new, reason = EDITS[n]
            changes.append({'query_id':q['id'], 'old_query':q['query'], 'new_query':new,'reason':reason})
            q['query'] = new
        q['judgment_status'] = 'codex_visual_reviewed_v2_no_human_double_review'
        q['judgment_scope'] = 'closed_corpus_all_137_visually_inspected'
        refs = q['relevant_photo_ids'] or q['hard_negative_photo_ids']
        q['family_id'] = 'photo-family-' + (find(refs[0]) if refs else q['id'])
        reviews.append({'query_id':q['id'], 'reviewed_corpus_size':len(corpus),
                        'positive_ids':q['relevant_photo_ids'], 'hard_negative_ids':q['hard_negative_photo_ids'],
                        'decision':'query_revised_labels_retained' if n in EDITS else 'retained_after_visual_review',
                        'evidence':{p:observations[p] for p in q['evidence_photo_ids']}})
    OUT.mkdir(parents=True,exist_ok=True)
    def save(p,obj): p.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    save(OUT/'corpus.json',corpus)
    (OUT/'queries.jsonl').write_text(''.join(json.dumps(q,ensure_ascii=False)+'\n' for q in queries),encoding='utf8')
    with (OUT/'qrels.tsv').open('w',encoding='utf8',newline='') as f:
        f.write('query_id\tphoto_id\trelevance\n')
        for q in queries:
            for p in corpus: f.write(f"{q['id']}\t{p['photo_id']}\t{int(p['photo_id'] in q['relevant_photo_ids'])}\n")
    save(OUT/'review.json',{'reviewer':'Codex visual inspection; no human double annotation',
          'reviewed_at':datetime.now(timezone.utc).isoformat(),'retrieval_outputs_seen':False,
          'method':'Read all 217 queries; inspect all 137 photos in nine sheets; compare visible conditions and confusable alternatives; zoom uncertain OCR, pen, vehicle color and silhouette.',
          'limitations':['Single AI reviewer; binary full-corpus judgments may still contain mistakes.','Images are synthetic and most queries have one positive; not representative personal-album accuracy.'],
          'changes':changes,'observations':observations,'query_reviews':reviews})
    save(OUT/'meta.json',{'version':'2.0.0-reviewed-development','photos':len(corpus),'queries':len(queries),'query_edits':len(changes),
          'positive_labels_changed':0,'original_query_sha256':hashlib.sha256((old/'queries.jsonl').read_bytes()).hexdigest()})
    print(json.dumps({'photos':len(corpus),'queries':len(queries),'edits':len(changes)}))

if __name__ == '__main__': main()
