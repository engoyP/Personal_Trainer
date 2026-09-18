// WGS-84（GPS 原始坐标）↔ GCJ-02（火星坐标，国内地图通用）
// 手表和手机记录的是 WGS-84，直接画到腾讯/高德地图上会偏移几百米，必须先转换。

const PI = Math.PI
const A = 6378245.0
const EE = 0.00669342162296594323

function outOfChina(lat, lon) {
  return lon < 72.004 || lon > 137.8347 || lat < 0.8293 || lat > 55.8271
}

function transformLat(x, y) {
  let ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x))
  ret += ((20.0 * Math.sin(6.0 * x * PI) + 20.0 * Math.sin(2.0 * x * PI)) * 2.0) / 3.0
  ret += ((20.0 * Math.sin(y * PI) + 40.0 * Math.sin((y / 3.0) * PI)) * 2.0) / 3.0
  ret += ((160.0 * Math.sin((y / 12.0) * PI) + 320 * Math.sin((y * PI) / 30.0)) * 2.0) / 3.0
  return ret
}

function transformLon(x, y) {
  let ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x))
  ret += ((20.0 * Math.sin(6.0 * x * PI) + 20.0 * Math.sin(2.0 * x * PI)) * 2.0) / 3.0
  ret += ((20.0 * Math.sin(x * PI) + 40.0 * Math.sin((x / 3.0) * PI)) * 2.0) / 3.0
  ret += ((150.0 * Math.sin((x / 12.0) * PI) + 300.0 * Math.sin((x / 30.0) * PI)) * 2.0) / 3.0
  return ret
}

/** @returns {[number, number]} [lat, lon] in GCJ-02 */
export function wgs84ToGcj02(lat, lon) {
  if (outOfChina(lat, lon)) return [lat, lon]

  let dLat = transformLat(lon - 105.0, lat - 35.0)
  let dLon = transformLon(lon - 105.0, lat - 35.0)

  const radLat = (lat / 180.0) * PI
  let magic = Math.sin(radLat)
  magic = 1 - EE * magic * magic
  const sqrtMagic = Math.sqrt(magic)

  dLat = (dLat * 180.0) / (((A * (1 - EE)) / (magic * sqrtMagic)) * PI)
  dLon = (dLon * 180.0) / ((A / sqrtMagic) * Math.cos(radLat) * PI)

  return [lat + dLat, lon + dLon]
}

/** 把任意形态的点（[lat,lon] 或 {lat,lon}）统一转成 GCJ-02 的 [lat, lon] */
export function toGcjPoints(points) {
  return (points || [])
    .map((p) => (Array.isArray(p) ? [p[0], p[1]] : [p.lat, p.lon]))
    .filter(([lat, lon]) => typeof lat === 'number' && typeof lon === 'number')
    .map(([lat, lon]) => wgs84ToGcj02(lat, lon))
}
