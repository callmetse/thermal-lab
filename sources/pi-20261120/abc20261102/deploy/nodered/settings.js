module.exports = {
  flowFile: process.env.ABC_FLOW_FILE || 'flows.json',
  uiPort: Number(process.env.ABC_UI_PORT || 1880),
  uiHost: process.env.ABC_UI_HOST || '0.0.0.0',
  httpAdminRoot: false,
  httpNodeAuth: {user: process.env.ABC_UI_USER, pass: process.env.ABC_UI_PASSWORD_HASH},
  functionGlobalContext: {crypto: require('crypto'), zlib: require('zlib'), perf: require('perf_hooks').performance,
    console_boot_id: require('crypto').randomUUID()},
  contextStorage: {default: {module: 'memory'}},
  logging: {console: {level: 'info', metrics: false, audit: false}},
};
