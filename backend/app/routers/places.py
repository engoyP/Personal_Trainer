"""去过的城市：地图点亮。

设计要点：
  - 城市基础数据内置（省会 + 主要地级市，GCJ-02 坐标），不依赖外部接口，离线可用；
  - 手动标记：从运动详情页添加，城市名唯一（重复标记累加 visit_count）；
  - 只存城市级坐标，不存逐点轨迹——个人位置数据仅本地私有存储，不外传。
"""
from __future__ import annotations

import re
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.fitness import Activity, VisitedCity

router = APIRouter(prefix="/api/places", tags=["places"])

# 中国主要城市（省会 + 主要地级市 / 计划单列市），GCJ-02 坐标（腾讯地图用）
# 格式：(城市名, 省/直辖市, 纬度, 经度)
_CITIES: list[tuple[str, str, float, float]] = [
    ("北京", "北京", 39.9042, 116.4074),
    ("上海", "上海", 31.2304, 121.4737),
    ("广州", "广东", 23.1291, 113.2644),
    ("深圳", "广东", 22.5431, 114.0579),
    ("天津", "天津", 39.3434, 117.3616),
    ("重庆", "重庆", 29.5630, 106.5516),
    ("成都", "四川", 30.5728, 104.0668),
    ("杭州", "浙江", 30.2741, 120.1551),
    ("武汉", "湖北", 30.5928, 114.3055),
    ("西安", "陕西", 34.3416, 108.9398),
    ("南京", "江苏", 32.0603, 118.7969),
    ("苏州", "江苏", 31.2989, 120.5853),
    ("郑州", "河南", 34.7466, 113.6254),
    ("长沙", "湖南", 28.2282, 112.9388),
    ("青岛", "山东", 36.0671, 120.3826),
    ("济南", "山东", 36.6512, 117.1201),
    ("沈阳", "辽宁", 41.8057, 123.4315),
    ("大连", "辽宁", 38.9140, 121.6147),
    ("哈尔滨", "黑龙江", 45.8038, 126.5349),
    ("长春", "吉林", 43.8171, 125.3235),
    ("石家庄", "河北", 38.0428, 114.5149),
    ("太原", "山西", 37.8706, 112.5489),
    ("呼和浩特", "内蒙古", 40.8414, 111.7519),
    ("兰州", "甘肃", 36.0611, 103.8343),
    ("西宁", "青海", 36.6171, 101.7782),
    ("银川", "宁夏", 38.4872, 106.2309),
    ("乌鲁木齐", "新疆", 43.8256, 87.6168),
    ("拉萨", "西藏", 29.6520, 91.1721),
    ("昆明", "云南", 25.0389, 102.7183),
    ("贵阳", "贵州", 26.6470, 106.6302),
    ("南宁", "广西", 22.8170, 108.3665),
    ("海口", "海南", 20.0444, 110.1999),
    ("三亚", "海南", 18.2528, 109.5119),
    ("福州", "福建", 26.0745, 119.2965),
    ("厦门", "福建", 24.4798, 118.0894),
    ("南昌", "江西", 28.6820, 115.8579),
    ("合肥", "安徽", 31.8206, 117.2272),
    ("宁波", "浙江", 29.8683, 121.5440),
    ("温州", "浙江", 27.9943, 120.6994),
    ("无锡", "江苏", 31.4912, 120.3119),
    ("佛山", "广东", 23.0215, 113.1219),
    ("东莞", "广东", 23.0207, 113.7518),
    ("珠海", "广东", 22.2710, 113.5767),
    ("洛阳", "河南", 34.6197, 112.4540),
    ("宜昌", "湖北", 30.6919, 111.2861),
    ("桂林", "广西", 25.2736, 110.2900),
    ("丽江", "云南", 26.8721, 100.2299),
    ("大理", "云南", 25.6065, 100.2676),
    ("秦皇岛", "河北", 39.9354, 119.6005),
    ("烟台", "山东", 37.4638, 121.4479),
    ("扬州", "江苏", 32.3931, 119.4215),
    ("镇江", "江苏", 32.1878, 119.4250),
    ("绍兴", "浙江", 30.0300, 120.5800),
    ("嘉兴", "浙江", 30.7466, 120.7556),
    ("汕头", "广东", 23.3541, 116.6819),
    ("湛江", "广东", 21.2710, 110.3579),
    ("惠州", "广东", 23.1115, 114.4166),
    ("中山", "广东", 22.5210, 113.3926),
    ("保定", "河北", 38.8739, 115.4646),
    ("廊坊", "河北", 39.5379, 116.6835),
    ("唐山", "河北", 39.6304, 118.1804),
    ("大同", "山西", 40.0904, 113.3001),
    ("包头", "内蒙古", 40.6578, 109.8403),
    ("鞍山", "辽宁", 41.1082, 122.9946),
    ("吉林", "吉林", 43.8401, 126.5496),
    ("大庆", "黑龙江", 46.5872, 125.1146),
    ("徐州", "江苏", 34.2618, 117.1581),
    ("常州", "江苏", 31.8113, 119.9740),
    ("南通", "江苏", 31.9802, 120.8943),
    ("泉州", "福建", 24.8740, 118.6757),
    ("漳州", "福建", 24.5129, 117.6478),
    ("九江", "江西", 29.7062, 116.0000),
    ("赣州", "江西", 25.8293, 114.9350),
    ("株洲", "湖南", 27.8274, 113.1330),
    ("湘潭", "湖南", 27.8296, 112.9440),
    ("衡阳", "湖南", 26.8946, 112.5730),
    ("襄阳", "湖北", 32.0420, 112.1440),
    ("开封", "河南", 34.7973, 114.3073),
    ("新乡", "河南", 35.3030, 113.9260),
    ("潍坊", "山东", 36.7069, 119.1619),
    ("淄博", "山东", 36.8131, 118.0550),
    ("临沂", "山东", 35.1047, 118.3564),
    ("威海", "山东", 37.5133, 122.1203),
    ("绵阳", "四川", 31.4675, 104.6795),
    ("宜宾", "四川", 28.7513, 104.6230),
    ("遵义", "贵州", 27.7254, 106.9272),
    ("柳州", "广西", 24.3264, 109.4281),
    ("北海", "广西", 21.4733, 109.1197),
    ("宝鸡", "陕西", 34.3616, 107.2372),
    ("咸阳", "陕西", 34.3296, 108.7080),
    ("天水", "甘肃", 34.5809, 105.7249),
    ("香港", "香港", 22.3193, 114.1694),
    ("澳门", "澳门", 22.1987, 113.5439),
    ("台北", "台湾", 25.0330, 121.5654),
    ("高雄", "台湾", 22.6273, 120.3014),
]


class CityOut(BaseModel):
    name: str
    province: str | None = None
    lat: float
    lng: float


class VisitIn(BaseModel):
    city_name: str = Field(..., min_length=1, max_length=64)
    activity_id: int | None = None
    first_date: str | None = None      # YYYY-MM-DD，缺省取关联运动的日期


class VisitOut(BaseModel):
    id: int
    city_name: str
    province: str | None
    lat: float
    lng: float
    first_date: str | None
    activity_id: int | None
    visit_count: int


def _find_city(name: str) -> tuple[str, str, float, float] | None:
    """按城市名精确/模糊匹配内置城市库。"""
    key = name.strip()
    if not key:
        return None
    for c in _CITIES:
        if c[0] == key:
            return c
    # 去掉「市」后缀再试
    short = key[:-1] if key.endswith("市") else key
    for c in _CITIES:
        if c[0] == short or c[0].startswith(short) or short.startswith(c[0]):
            return c
    return None


@router.get("/cities", response_model=list[CityOut])
def list_cities(q: str | None = None, limit: int = 30):
    """内置城市库搜索（给前端下拉选城市用）。"""
    items = [CityOut(name=c[0], province=c[1], lat=c[2], lng=c[3]) for c in _CITIES]
    if q:
        k = q.strip()
        items = [c for c in items if k in c.name or (c.province and k in c.province)]
    return items[:limit]


@router.get("/visited", response_model=list[VisitOut])
def list_visited(db: Session = Depends(get_db)):
    """已点亮的城市列表（按首次达成日期倒序）。"""
    rows = db.query(VisitedCity).order_by(VisitedCity.created_at.desc()).all()
    return [
        VisitOut(
            id=r.id,
            city_name=r.city_name,
            province=r.province,
            lat=r.lat,
            lng=r.lng,
            first_date=r.first_date.isoformat() if r.first_date else None,
            activity_id=r.activity_id,
            visit_count=r.visit_count or 1,
        )
        for r in rows
    ]


@router.post("/visited", response_model=VisitOut)
def add_visited(payload: VisitIn, db: Session = Depends(get_db)):
    """点亮一个城市（重复标记只累加次数）。"""
    city = _find_city(payload.city_name)
    if not city:
        raise HTTPException(
            status_code=400,
            detail=f"城市库里没有「{payload.city_name}」，请从下拉列表里选一个",
        )
    name, province, lat, lng = city

    # 首次达成日期：优先用传入值，其次用关联运动的开始时间
    first_date: date | None = None
    if payload.first_date:
        try:
            first_date = datetime.strptime(payload.first_date[:10], "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="first_date 格式应为 YYYY-MM-DD")
    activity_id = payload.activity_id
    if first_date is None and activity_id:
        act = db.get(Activity, activity_id)
        if act and act.start_time:
            first_date = act.start_time.date()

    row = db.query(VisitedCity).filter(VisitedCity.city_name == name).first()
    if row:
        row.visit_count = (row.visit_count or 1) + 1
        if first_date and (row.first_date is None or first_date < row.first_date):
            row.first_date = first_date
        db.commit()
        db.refresh(row)
    else:
        row = VisitedCity(
            city_name=name,
            province=province,
            lat=lat,
            lng=lng,
            first_date=first_date,
            activity_id=activity_id,
            visit_count=1,
        )
        db.add(row)
        db.commit()
        db.refresh(row)

    return VisitOut(
        id=row.id,
        city_name=row.city_name,
        province=row.province,
        lat=row.lat,
        lng=row.lng,
        first_date=row.first_date.isoformat() if row.first_date else None,
        activity_id=row.activity_id,
        visit_count=row.visit_count or 1,
    )


@router.delete("/visited/{city_id}")
def delete_visited(city_id: int, db: Session = Depends(get_db)):
    """取消点亮。"""
    row = db.get(VisitedCity, city_id)
    if not row:
        raise HTTPException(status_code=404, detail="记录不存在")
    db.delete(row)
    db.commit()
    return {"ok": True, "city_name": row.city_name}
