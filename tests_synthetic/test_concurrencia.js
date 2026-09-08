const { chromium } = require('playwright-core');
const APP = 'http://localhost:8502';
const DATA = 'c:/Users/artur/Desktop/rental/gastos zips/organizador-facturas/tests_synthetic/data';
const CSV = 'c:/Users/artur/Desktop/rental/gastos zips/organizador-facturas/tests_synthetic/propiedades_test.csv';

const pendientes = async (p) => {
  const h = p.getByText(/Revisar a mano \(\d+ pendientes\)/);
  return (await h.count()) ? (await h.first().innerText()).match(/\d+/)[0] : '0';
};

(async () => {
  const b = await chromium.launch({ channel: 'msedge' });

  // Sesion A: procesa el lote sintetico desde CARPETA (input nuevo), no desde ZIP.
  const a = await (await b.newContext()).newPage();
  await a.goto(APP, { waitUntil: 'networkidle' });
  await a.locator('input[type=file]').nth(1).setInputFiles(CSV);
  await a.waitForTimeout(3000);
  const ruta = a.getByRole('textbox', { name: /ruta de una carpeta/ });
  await ruta.fill(DATA);
  await ruta.press('Enter');
  await a.waitForTimeout(3000);
  await a.getByRole('button', { name: 'Procesar facturas' }).click();
  await a.getByText(/Revisar a mano/).waitFor({ timeout: 180000 });
  console.log('A tras procesar carpeta:', await pendientes(a), 'pendientes');

  // Sesion B: segunda persona, abierta ANTES de que A asigne nada.
  const bb = await (await b.newContext()).newPage();
  await bb.goto(APP, { waitUntil: 'networkidle' });
  await bb.waitForTimeout(4000);
  console.log('B al abrir:', await pendientes(bb), 'pendientes');

  // A asigna una factura.
  const factura = await bb.locator('div[data-baseweb="select"]').first().innerText();
  await a.getByRole('combobox', { name: 'Pertenece a' }).click();
  await a.getByRole('option').first().click();
  await a.getByRole('button', { name: 'Asignar' }).click();
  await a.waitForTimeout(6000);
  console.log('A tras asignar:', await pendientes(a), 'pendientes');

  // B NO recarga: solo navega. Si el estado es compartido, ve la lista ya al dia.
  await bb.getByRole('button', { name: /Siguiente/ }).click();
  await bb.waitForTimeout(6000);
  console.log('B tras navegar (sin recargar):', await pendientes(bb), 'pendientes');
  await bb.screenshot({ path: 'concurrencia_b.png' });

  await b.close();
})();
