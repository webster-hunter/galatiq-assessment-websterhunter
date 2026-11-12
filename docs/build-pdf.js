// Rebuilds docs/design-and-demo.pdf from docs/design-and-demo.html:  node docs/build-pdf.js
// Needs Playwright and its Chromium (npm install playwright; npx playwright install chromium).
const path = require('path');
const { pathToFileURL } = require('url');
const { chromium } = require('playwright');

(async () => {
  const source = path.join(__dirname, 'design-and-demo.html');
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto(pathToFileURL(source).href, { waitUntil: 'load' });
  await page.pdf({
    path: path.join(__dirname, 'design-and-demo.pdf'),
    format: 'A4',
    printBackground: true,
    displayHeaderFooter: true,
    headerTemplate: '<span></span>',
    footerTemplate: '<div style="font-size:8px;color:#7b8698;width:100%;padding:0 16mm;display:flex;justify-content:space-between;">'
      + '<span>Invoice processing for Acme Corp: design and demo notes</span>'
      + '<span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>',
    margin: { top: '18mm', bottom: '18mm', left: '16mm', right: '16mm' },
  });
  await browser.close();
})();
