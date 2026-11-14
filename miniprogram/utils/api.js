// 请求封装: 统一处理后端 (ok/http_status/msg/data) 风格与 HTTP 状态码
// 返回 Promise<{ok, status, data|detail}>; 网络失败 status=0
function request(path, opts) {
  const app = getApp();
  const base = (app && app.globalData && app.globalData.apiBase)
    || "http://localhost:8000";
  opts = opts || {};
  return new Promise(function (resolve) {
    wx.request({
      url: base + path,
      method: opts.method || "GET",
      data: opts.data,
      header: { "Content-Type": "application/json" },
      success: function (res) {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          resolve({ ok: true, status: res.statusCode, data: res.data });
        } else {
          resolve({
            ok: false,
            status: res.statusCode,
            detail: (res.data && res.data.detail)
              || ("HTTP " + res.statusCode)
          });
        }
      },
      fail: function (err) {
        resolve({
          ok: false,
          status: 0,
          detail: "网络错误：" + (err && err.errMsg || "无法连接后端")
        });
      }
    });
  });
}

module.exports = { request: request };
