import { test, expect } from './fixtures';

test('official preview, keyboard-authored workflow, validation, activation, and return to official mode', async ({ studio }) => {
  const { page } = studio;
  await expect(page.getByText('官方流程 v1.0')).toBeVisible();
  await expect(page.getByText('只读预览')).toBeVisible();

  await page.getByRole('button', { name: '复制为自定义流程' }).click();
  await expect(page.getByText('已在浏览器中创建草稿；保存前不会更改项目文件。')).toBeVisible();
  await page.reload();
  await expect(page.getByText('只读预览')).toBeVisible();
  await page.getByRole('button', { name: '新建空白流程' }).click();

  await page.getByLabel('主要 Skill').selectOption('paper-test-skill');
  await page.getByRole('button', { name: /向右移动阶段/ }).click();
  await expect(page.getByText('最近编辑：仅画布位置')).toBeVisible();

  await page.getByRole('button', { name: /空白任务阶段/ }).click();
  await page.getByLabel('主要 Skill').selectOption('paper-test-skill');
  await page.getByLabel('从阶段').selectOption('step-1');
  await page.getByLabel('连接到').selectOption('stage-1');
  await page.getByRole('button', { name: '添加连接' }).click();

  await page.getByRole('button', { name: '验证流程' }).click();
  await expect(page.getByText('检查完成 · 有风险提示')).toBeVisible();
  await page.getByRole('button', { name: '保存草稿' }).click();
  await expect(page.getByText('草稿已保存在当前项目中；流程仍未启用。')).toBeVisible();

  await page.reload();
  await expect(page.getByText('检测到已保存的自定义草稿；默认官方流程仍处于启用状态。')).toBeVisible();
  await page.getByRole('button', { name: '打开草稿' }).click();
  await expect(page.getByText('2 阶段')).toBeVisible();
  await expect(page.getByRole('button', { name: /02 新任务阶段/ })).toBeVisible();

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
  await expect(page.getByText(/当前启用：.*哈希 [a-f0-9]{12}…/)).toBeVisible();

  page.once('dialog', async (dialog) => { await dialog.accept(); });
  await page.getByRole('button', { name: '切回官方流程' }).click();
  await expect(page.getByText('官方流程 v1.0')).toBeVisible();
  await page.reload();
  await expect(page.getByText('检测到已保存的自定义草稿；默认官方流程仍处于启用状态。')).toBeVisible();
});

test('two tabs cannot overwrite a newer saved revision', async ({ studio }) => {
  const { page, context, url } = studio;
  await page.getByRole('button', { name: '新建空白流程' }).click();
  await page.getByLabel('主要 Skill').selectOption('paper-test-skill');
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
