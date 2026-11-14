// SupplyGuard 待复核签收小程序 (阶段05板块3)
// 对接后端 /api/reviews 系列接口
App({
  globalData: {
    // 后端基址: 用 localhost 而非 127.0.0.1 —— 开发者工具对
    // http://127.0.0.1 会强制拦截明文请求, localhost 默认放行;
    // 后端需以 --host 0.0.0.0 启动 (uvicorn main:app --host 0.0.0.0 --port 8000);
    // 真机调试改为电脑局域网 IP (如 http://192.168.x.x:8000)
    apiBase: "http://localhost:8000"
  }
});
