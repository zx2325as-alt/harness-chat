/** @type {import('@vue/cli-service').ProjectOptions} */
module.exports = {
  devServer: {
    port: 6008,
    // 容器 / 云平台端口映射时用域名访问，否则 webpack-dev-server 会报 Invalid Host header
    host: "0.0.0.0",
    allowedHosts: "all",
  },
};
