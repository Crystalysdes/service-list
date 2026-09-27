-- The garant moves from Crypto Pay (@CryptoBot) to Apirone. The running version must finish everything that
-- still has money or a promise in Crypto Pay first: every line below is something to finish.
-- hint: /admin → 🛡 Гарант → «⏸ Остановить приём сделок», доведите эти сделки и выплаты до конца (или отмените неоплаченные) на работающей версии, затем запустите обновление снова
SELECT 'приём сделок гаранта включён'
FROM settings
WHERE key = 'escrow' AND jsonb_typeof(value) = 'object' AND value->>'enabled' = 'true'
UNION ALL
SELECT 'сделка #' || id || ' не завершена (' || status || ')'
FROM deals
WHERE status IN ('pending', 'awaiting_payment', 'funded', 'delivered', 'disputed', 'settling')
UNION ALL
SELECT 'выплата #' || id || ' по сделке #' || deal_id || ' не завершена (' || status || ')'
FROM deal_payouts
WHERE status NOT IN ('done', 'manual')
UNION ALL
SELECT 'счёт #' || id || ' по сделке #' || deal_id || ' ещё можно оплатить'
FROM deal_invoices
WHERE status = 'active';
