/** @type {import('@vue/cli-service').ProjectOptions} */
module.exports = {
  productionSourceMap: false,
  devServer: {
    port: 6008,
    // 容器 / 云平台端口映射时用域名访问，否则 webpack-dev-server 会报 Invalid Host header
    host: "0.0.0.0",
    allowedHosts: "all",
    // 开发时前端走同源 /api，避免写死 localhost:8000（线上 Failed to fetch）
    proxy: {
      "^/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        ws: true,
      },
    },
  },
};
