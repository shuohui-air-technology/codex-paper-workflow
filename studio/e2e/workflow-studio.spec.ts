import { test, expect } from './fixtures';

test('official preview, keyboard-authored workflow, validation, activation, and return to official mode', async ({ studio }, testInfo) => {
  const { page } = studio;
  await expect(page.getByText('官方流程 v1.0')).toBeVisible();
  await expect(page.getByText('只读预览')).toBeVisible();

  await page.getByRole('button', { name: '复制为自定义流程' }).click();
  await expect(page.getByText('已在浏览器中创建草稿；保存前不会更改项目文件。')).toBeVisible();
  await page.reload();
  await expect(page.getByText('只读预览')).toBeVisible();
  await page.getByRole('button', { name: '新建空白流程' }).click();

  await page.getByLabel('主要 Skill').selectOption('research-skill-router');
  await page.getByText('高级设置', { exact: true }).click();
  await page.getByRole('button', { name: /向右移动阶段/ }).click();
  await expect(page.getByText('最近编辑：仅画布位置')).toBeVisible();

  await page.getByRole('button', { name: /空白任务阶段/ }).click();
  await page.getByLabel('主要 Skill').selectOption('research-skill-router');
  await expect(page.locator('.react-flow__edge')).toHaveCount(1);
  await page.getByRole('button', { name: '删除阶段' }).click();
  await expect(page.getByText('1 阶段')).toBeVisible();
  await page.getByRole('button', { name: /撤销$/ }).click();
  await expect(page.getByText('2 阶段')).toBeVisible();
  await expect(page.locator('.react-flow__edge')).toHaveCount(1);
  await page.getByRole('button', { name: '自动整理画布' }).click();
  await page.getByRole('button', { name: '流程设置', exact: true }).click();
  await expect(page.getByLabel('最大并行阶段数')).toHaveValue('1');

  await page.getByRole('button', { name: '验证流程' }).click();
  await expect(page.getByText('检查完成 · 有风险提示')).toBeVisible();
  await page.getByLabel('流程标识').fill('');
  await page.getByLabel('流程标识').pressSequentially('introduction-review');
  await page.getByRole('button', { name: '保存草稿' }).click();
  await expect(page.getByText('草稿已保存在当前项目中；流程仍未启用。')).toBeVisible();

  await page.reload();
  await expect(page.getByText('检测到已保存的自定义草稿；默认官方流程仍处于启用状态。')).toBeVisible();
  await page.getByRole('button', { name: '打开草稿' }).click();
  await expect(page.getByText('2 阶段')).toBeVisible();
  await expect(page.getByRole('button', { name: /02 新任务阶段/ })).toBeVisible();
  await page.getByRole('button', { name: '流程设置', exact: true }).click();
  await expect(page.getByLabel('流程标识')).toHaveValue('introduction-review');

  await page.getByRole('button', { name: '验证并启用' }).click();
  const riskDialog = page.getByRole('dialog', { name: '请逐项确认流程变更' });
  await expect(riskDialog).toBeVisible();
  const riskCodes = await riskDialog.locator('code').allTextContents();
  expect(riskCodes.length).toBeGreaterThan(0);
  const acknowledgements = await riskDialog.getByRole('checkbox').all();
  expect(acknowledgements.length).toBe(riskCodes.length);
  for (const acknowledgement of acknowledgements) await acknowledgement.check();
  await riskDialog.getByRole('button', { name: '启用自定义流程' }).click();
  await expect(page.getByText('自定义流程已启用')).toBeVisible();
  await expect(page.getByRole('button', { name: '复制继续执行提示词' })).toBeVisible();
  await expect(page.getByText(/当前启用：.*哈希 [a-f0-9]{12}…/)).toBeVisible();
  await page.setViewportSize({ width: 1366, height: 768 });
  await page.mouse.move(1360, 700);
  await page.mouse.wheel(0, 700);
  await expect.poll(async () => {
    const panel = await page.locator('.validation-panel').boundingBox();
    return Boolean(panel && panel.y + panel.height <= 768);
  }).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('active-short-window.png'), fullPage: true, animations: 'disabled' });

  page.once('dialog', async (dialog) => { await dialog.accept(); });
  await page.getByRole('button', { name: '切回官方流程' }).click();
  await expect(page.getByText('官方流程 v1.0')).toBeVisible();
  await page.reload();
  await expect(page.getByText('检测到已保存的自定义草稿；默认官方流程仍处于启用状态。')).toBeVisible();
});

test('compact screens keep setup controls accessible and locate missing configuration', async ({ studio }, testInfo) => {
  const { page } = studio;
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: '新建空白流程' }).click();
  await page.getByRole('button', { name: '流程设置', exact: true }).click();
  await expect(page.getByRole('heading', { name: '流程设置' })).toBeVisible();
  await page.getByRole('button', { name: '验证流程' }).click();
  await page.getByRole('button', { name: '定位相关阶段' }).first().click();
  await expect(page.getByLabel('阶段名称')).toHaveValue('第一个任务阶段');
  expect(await page.locator('html').evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('mobile-editor.png'), fullPage: true, animations: 'disabled' });
});

test('two tabs cannot overwrite a newer saved revision', async ({ studio }) => {
  const { page, context, url } = studio;
  await page.getByRole('button', { name: '新建空白流程' }).click();
  await page.getByLabel('主要 Skill').selectOption('research-skill-router');
  await page.getByRole('button', { name: '保存草稿' }).click();
  await expect(page.getByText('草稿已保存在当前项目中；流程仍未启用。')).toBeVisible();

  const secondTab = await context.newPage();
  await secondTab.goto(url);
  await expect(secondTab.getByText('检测到已保存的自定义草稿；默认官方流程仍处于启用状态。')).toBeVisible();
  await secondTab.getByRole('button', { name: '打开草稿' }).click();

  await page.getByLabel('阶段名称').fill('Revision from first tab');
  await page.getByRole('button', { name: '保存草稿' }).click();
  await expect(page.getByText('草稿已保存在当前项目中；流程仍未启用。')).toBeVisible();

  await secondTab.getByLabel('阶段名称').fill('Revision from second tab');
  await secondTab.getByRole('button', { name: '保存草稿' }).click();
  await expect(secondTab.getByRole('dialog', { name: '服务器中的流程状态已经变化' })).toBeVisible();
  await expect(secondTab.getByRole('button', { name: '保存草稿' })).toBeDisabled();
  await secondTab.getByRole('button', { name: '继续保留当前草稿' }).click();
  await expect(secondTab.getByRole('button', { name: '保存草稿' })).toBeDisabled();
  await expect(secondTab.getByLabel('阶段名称')).toHaveValue('Revision from second tab');
});
