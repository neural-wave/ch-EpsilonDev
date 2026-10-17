const {test,expect} = require('@playwright/test');
const {boot,ready,ask,result,sidebar} = require('./fixtures');

for (const theme of ['light','dark']) {
  test(`${theme} theme keeps badge in view and page within viewport`,async ({page},testInfo) => {
    await boot(page,{reply:result(['category','patient_count'],Array.from({length:80},(_,i) => ({group:`group ${i}`,patients:'10'})),['group','patients'])});
    await ready(page);
    if (theme === 'dark') await page.locator('#theme-btn').click();
    await expect(page.locator('.privacy strong')).toBeInViewport();
    await ask(page);
    for (const position of ['top','bottom']) {
      await page.evaluate((position) => window.scrollTo(0,position === 'top' ? 0 : document.body.scrollHeight),position);
      await expect(page.locator('.privacy strong')).toBeInViewport();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    }
    await page.screenshot({path:testInfo.outputPath(`synthetic-${theme}.png`),fullPage:false});
  });
}

test('keyboard reaches import, pagination, and exports',async ({page}) => {
  await boot(page,{reply:result(['category','patient_count'],Array.from({length:51},(_,i) => ({group:String(i),patients:'5'})),['group','patients'])});
  await ready(page); await sidebar(page);
  await page.locator('#snapshot-import').focus(); await expect(page.locator('#snapshot-import')).toBeFocused();
  if (await page.locator('#scrim').isVisible()) await page.locator('#scrim').click();
  await ask(page);
  const next = page.getByRole('button',{name:'Next',exact:true});
  await next.focus(); await next.press('Enter');
  await expect(page.locator('.result-table-wrap')).toContainText('51–51 of 51');
  const csv = page.getByRole('button',{name:'Download CSV',exact:true});
  await csv.focus(); await expect(csv).toBeFocused();
});
