const {defineConfig} = require('@playwright/test');
const path = require('node:path');
const repo = path.resolve(__dirname, '../..');
const python = process.env.BROWSER_TEST_PYTHON || 'python';
// Quote the interpreter as shell data; a path may contain spaces.
const quotedPython = "'" + python.replace(/'/g, "'\\''") + "'";
module.exports = defineConfig({
  testDir: __dirname,
  testMatch: '*.spec.js',
  workers: 1,
  timeout: 30000,
  expect: {timeout: 5000},
  outputDir: 'artifacts/test-results',
  reporter: [['list'], ['junit', {outputFile:'artifacts/junit.xml'}], ['html', {outputFolder:'artifacts/report', open:'never'}]],
  use: {baseURL:'http://127.0.0.1:18081', browserName:'chromium', trace:'retain-on-failure', screenshot:'only-on-failure'},
  projects: [
    {name:'desktop', use:{viewport:{width:1280,height:900}}},
    {name:'phone', use:{viewport:{width:390,height:844}, isMobile:true, hasTouch:true}},
  ],
  webServer: {
    command:`${quotedPython} -m nl2sparql --host 127.0.0.1 --port 18081`,
    cwd:repo,
    env:{NL2SPARQL_BACKEND:'stub', DEMO_MIN_GROUP_SIZE:'disabled', PYTHONPATH:path.join(repo,'src')},
    url:'http://127.0.0.1:18081/health',
    reuseExistingServer:false,
    timeout:30000,
    gracefulShutdown:{signal:'SIGTERM',timeout:1000},
  },
});
