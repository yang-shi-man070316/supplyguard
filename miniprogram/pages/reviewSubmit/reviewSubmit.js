// 页面2: 签收提交表单页 (阶段05板块3)
// 对接 POST /api/reviews/{review_id}/receipt
// 规则: receipt_id 前端生成 (REC-xxxx); 三元组与 review 记录强制一致;
//       已 completed 复核单后端 409 拒绝 (页面亦前置拦截)
const api = require("../../utils/api.js");

const VERDICT = {
  established:          { cls: "badge-good", word: "成立" },
  blocked:              { cls: "badge-bad",  word: "受阻" },
  pending_verification: { cls: "badge-warn", word: "待验证" },
  unknown:              { cls: "badge-flat", word: "未知" }
};

function pad2(n) { return (n < 10 ? "0" : "") + n; }

// 前端生成唯一 receipt_id: REC-xxxxxxxx (8 位随机大写字母数字, 后端 409 幂等兜底)
function genReceiptId() {
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";
  let tail = "";
  for (let i = 0; i < 8; i++) {
    tail += chars[Math.floor(Math.random() * chars.length)];
  }
  return "REC-" + tail;
}

function nowDate() {
  const d = new Date();
  return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
}

function nowTime() {
  const d = new Date();
  return pad2(d.getHours()) + ":" + pad2(d.getMinutes());
}

Page({
  data: {
    // 复核单上下文 (URL 参数 + pending 列表核对)
    reviewId: "",
    dispatchId: "",
    serviceId: "",
    materialId: "",
    triggerRunId: "",
    createdText: "",

    // 表单
    qty: "",
    obsDate: "",
    obsTime: "",
    note: "",
    receiptId: "",

    // 状态
    warning: "",
    error: "",
    formErr: "",
    canSubmit: false,
    submitting: false,
    busyText: "提交中：写签收 → 关闭复核单 → 新建 run 并执行 T1~T5…",
    success: false,
    newRunId: "",
    tasks: []
  },

  onLoad: function (options) {
    options = options || {};
    const that = this;
    this.setData({
      reviewId: options.review_id || "",
      dispatchId: options.dispatch_id || "",
      serviceId: options.service_id || "",
      materialId: options.material_id || "",
      receiptId: genReceiptId(),
      obsDate: nowDate(),
      obsTime: nowTime()
    });
    if (!this.data.reviewId) {
      this.setData({
        formErr: "缺少 review_id 参数，请从待复核列表进入本页。",
        canSubmit: false
      });
      return;
    }
    // 用 pending 列表核对该复核单当前状态 (列表数据为权威来源)
    return api.request("/api/reviews/pending").then(function (res) {
      if (!res.ok) {
        that.setData({
          error: "无法连接后端获取复核单状态：" + res.detail,
          canSubmit: false
        });
        return;
      }
      const reviews = res.data.reviews || [];
      const hit = reviews.filter(function (r) {
        return r.review_id === that.data.reviewId;
      })[0];
      if (!hit) {
        that.setData({
          warning: "该复核单已不在待复核列表中（可能已签收完成），提交将被后端 409 拒绝。",
          canSubmit: false
        });
        return;
      }
      that.setData({
        triggerRunId: hit.trigger_run_id || "",
        createdText: (hit.created_at || "").replace("T", " ").slice(0, 19),
        canSubmit: that.validate()
      });
    });
  },

  validate: function () {
    const qty = Number(this.data.qty);
    return this.data.qty !== "" && Number.isInteger(qty) && qty >= 0
      && !!this.data.obsDate && !!this.data.obsTime;
  },

  onQtyInput: function (e) {
    this.setData({ qty: e.detail.value });
    this.setData({ canSubmit: this.validate() });
  },

  onDateChange: function (e) {
    this.setData({ obsDate: e.detail.value });
    this.setData({ canSubmit: this.validate() });
  },

  onTimeChange: function (e) {
    this.setData({ obsTime: e.detail.value });
    this.setData({ canSubmit: this.validate() });
  },

  onNoteInput: function (e) {
    this.setData({ note: e.detail.value });
  },

  submit: function () {
    const that = this;
    if (this.data.submitting || this.data.warning) { return; }
    const qty = Number(this.data.qty);
    if (!this.validate()) {
      this.setData({ formErr: "实收数量必须为非负整数，且接收时间不能为空。" });
      return;
    }
    this.setData({ submitting: true, formErr: "", error: "" });
    const payload = {
      receipt_id: this.data.receiptId,
      dispatch_id: this.data.dispatchId,
      service_id: this.data.serviceId,
      material_id: this.data.materialId,
      received_quantity: qty,
      observed_at: this.data.obsDate + "T" + this.data.obsTime + ":00",
      source_type: "API",
      note: this.data.note || undefined
    };
    return api.request(
      "/api/reviews/" + encodeURIComponent(this.data.reviewId) + "/receipt",
      { method: "POST", data: payload }
    ).then(function (res) {
      if (!res.ok) {
        that.setData({
          submitting: false,
          error: "提交失败：" + res.detail
        });
        return;
      }
      const tasks = (res.data.execute && res.data.execute.tasks || []).map(
        function (t) {
          const v = VERDICT[t.result_status] || VERDICT.unknown;
          return {
            task_id: t.task_id,
            task_name: t.task_name || t.task_id,
            cls: v.cls,
            word: v.word
          };
        }
      );
      that.setData({
        submitting: false,
        success: true,
        newRunId: res.data.new_run_id || "-",
        receiptId: res.data.receipt_id || that.data.receiptId,
        tasks: tasks
      });
      wx.setNavigationBarTitle({ title: "签收成功" });
    });
  },

  goBack: function () {
    wx.navigateBack({
      fail: function () {
        wx.redirectTo({ url: "/pages/reviewList/reviewList" });
      }
    });
  }
});
