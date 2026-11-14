// 页面1: 待复核列表页 (阶段05板块3)
// 对接 GET /api/reviews/pending; 点击条目跳转签收提交表单页
const api = require("../../utils/api.js");

function pad2(n) { return (n < 10 ? "0" : "") + n; }

Page({
  data: {
    reviews: [],
    loading: false,
    error: "",
    updatedAt: ""
  },

  onShow: function () {
    // onShow 保证从表单页返回时自动刷新待办
    return this.fetchPending();
  },

  onPullDownRefresh: function () {
    return this.fetchPending();
  },

  fetchPending: function () {
    const that = this;
    that.setData({ loading: true, error: "" });
    return api.request("/api/reviews/pending").then(function (res) {
      if (!res.ok) {
        that.setData({
          loading: false,
          error: "获取待复核列表失败：" + res.detail
        });
        return;
      }
      const reviews = (res.data.reviews || []).map(function (r) {
        r.timeText = (r.created_at || "").replace("T", " ").slice(0, 16);
        return r;
      });
      that.setData({
        reviews: reviews,
        loading: false,
        updatedAt: that.nowText()
      });
      if (wx.stopPullDownRefresh) { wx.stopPullDownRefresh(); }
    });
  },

  // 点击条目 → 跳转签收提交表单页 (携带 review 上下文参数)
  openReview: function (e) {
    const r = this.data.reviews[e.currentTarget.dataset.index];
    if (!r) { return; }
    wx.navigateTo({
      url: "/pages/reviewSubmit/reviewSubmit"
        + "?review_id=" + encodeURIComponent(r.review_id)
        + "&dispatch_id=" + encodeURIComponent(r.dispatch_id)
        + "&service_id=" + encodeURIComponent(r.service_id)
        + "&material_id=" + encodeURIComponent(r.material_id)
    });
  },

  nowText: function () {
    const d = new Date();
    return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate())
      + " " + pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
  }
});
