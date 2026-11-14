import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/whitelist'
const columns = [
  "登记编号",
  "应用名称",
  "调用标识",
  "日配额上限",
  "优先级",
  "生效日期",
  "失效日期",
  "登记状态"
]
const actions = [
  "启用登记",
  "冻结来源",
  "注销来源"
]
const statuses = [
  "草稿",
  "已登记",
  "已冻结",
  "已注销"
]

type Row = Record<string, string | number | null>

type QuotaResult = {
  业务日期: string
  请求量: number
  配额消耗: number
  剩余额度: number
  是否超限: boolean
  来源状态: string
  明细条数: number
  说明: string
}

type GroupResult = {
  应用名称: string
  调用标识: string
  登记编号: string
  来源状态: string
  已失效: boolean
  成功: boolean
  失败原因: string | null
  处理日期: string[]
  跳过日期: string[]
  配额结果: QuotaResult[]
}

type BatchResult = {
  批次编号: string
  开始日期: string
  结束日期: string
  时区口径: string
  状态: string
  创建时间: string
  完成时间: string | null
  分组结果: GroupResult[]
}

type Checkpoint = {
  id: number
  应用名称: string
  调用标识: string
  登记编号: string
  已完成位置: number
  上一结算日: string
  上一日配额水位: number
  已失效: boolean
  最后结算时间: string
}

type LedgerRow = {
  id: number
  登记编号: string
  应用名称: string
  调用标识: string
  业务日期: string
  日配额上限: number
  请求总量: number
  配额消耗: number
  剩余额度: number
  是否超限: boolean
  来源状态: string
  明细条数: number
}

function statusBadge(status: string): string {
  if (status === '已登记') return 'badge badge-ok'
  if (status === '已冻结') return 'badge badge-frozen'
  if (status === '已注销') return 'badge badge-revoked'
  return 'badge badge-draft'
}

export default function WhitelistView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [startDate, setStartDate] = useState('2026-11-10')
  const [endDate, setEndDate] = useState('2026-11-12')
  const [settling, setSettling] = useState(false)
  const [batch, setBatch] = useState<BatchResult | null>(null)
  const [checkpoints, setCheckpoints] = useState<Checkpoint[]>([])
  const [ledger, setLedger] = useState<LedgerRow[]>([])
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('来源登记列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '调用来源管理列表读取失败')
    }
  }, [filters])

  const reloadSettlementData = useCallback(async () => {
    try {
      const [cpResp, ledgerResp] = await Promise.all([
        request(`${ENDPOINT}/checkpoints`),
        request(`${ENDPOINT}/quota-ledger`),
      ])
      if (cpResp.ok) {
        const cpPayload = await cpResp.json()
        setCheckpoints(cpPayload.items ?? [])
      }
      if (ledgerResp.ok) {
        const ledgerPayload = await ledgerResp.json()
        setLedger(ledgerPayload.items ?? [])
      }
    } catch {
      // 对账数据读取失败不阻塞主列表，页脚会保留上一次的错误提示
    }
  }, [])

  useEffect(() => {
    void reload()
    void reloadSettlementData()
  }, [reload, reloadSettlementData])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? '调用来源管理动作未生效，请稍后重试')
      }
      await Promise.all([reload(), reloadSettlementData()])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '调用来源管理操作失败')
    }
  }

  function toggleRow(id: number) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) {
        next.delete(id)
      } else {
        next.add(id)
      }
      return next
    })
  }

  function toggleAll() {
    setSelected((prev) => (
      prev.size === rows.length ? new Set() : new Set(rows.map((row) => Number(row.id)))
    ))
  }

  async function runBatchSettlement() {
    setErrorMessage('')
    if (selected.size === 0) {
      setErrorMessage('请先勾选需要结算的来源登记')
      return
    }
    if (!startDate || !endDate) {
      setErrorMessage('请选择完整的结算日期范围')
      return
    }
    setSettling(true)
    try {
      const response = await request(`${ENDPOINT}/settlements`, {
        method: 'POST',
        body: JSON.stringify({
          entry_ids: Array.from(selected),
          start_date: startDate,
          end_date: endDate,
        }),
      })
      const payload = await response.json()
      if (!response.ok) {
        // 409：同一调用标识已有批次在执行，稍后从断点重试即可，已结算部分不会重复扣减
        throw new Error(payload.detail ?? '跨日批量结算失败，请从断点重试')
      }
      setBatch(payload as BatchResult)
      await Promise.all([reload(), reloadSettlementData()])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '跨日批量结算失败')
    } finally {
      setSettling(false)
    }
  }

  const stats = useMemo(() => [
    { label: '来源登记总数', value: rows.length },
    { label: '已登记来源', value: rows.filter((row) => row.status === '已登记').length },
    { label: '已冻结来源', value: rows.filter((row) => row.status === '已冻结').length },
    { label: '已注销来源', value: rows.filter((row) => row.status === '已注销').length },
  ], [rows])

  return (
    <section className="page" data-module="whitelist">
      <header className="page-head">
        <div>
          <h2>调用来源管理</h2>
          <p className="page-desc">
            维护调用来源的标识登记、配额分配与有效期管理；跨日批量结算按应用名称与调用标识分段，
            断点水位保证失败可续算、重复结算不翻倍（时区口径 UTC+8）。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出调用来源管理清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <div className="settle-bar">
        <strong>跨日批量结算</strong>
        <label className="filter-item">
          <span>开始日期</span>
          <input type="date" value={startDate} onChange={(event) => setStartDate(event.target.value)} />
        </label>
        <label className="filter-item">
          <span>结束日期</span>
          <input type="date" value={endDate} onChange={(event) => setEndDate(event.target.value)} />
        </label>
        <button
          className="btn primary"
          type="button"
          disabled={settling}
          onClick={() => void runBatchSettlement()}
        >
          {settling ? '结算中…' : `结算选中 ${selected.size} 条来源`}
        </button>
        <span className="settle-hint">同一调用标识并发只允许一个批次成功，失败后可凭断点重跑且不重复扣减</span>
      </div>

      <table className="data-table">
        <thead>
          <tr>
            <th className="check-cell">
              <input
                type="checkbox"
                checked={rows.length > 0 && selected.size === rows.length}
                onChange={toggleAll}
                aria-label="全选来源登记"
              />
            </th>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)} className={selected.has(Number(row.id)) ? 'row-selected' : ''}>
              <td className="check-cell">
                <input
                  type="checkbox"
                  checked={selected.has(Number(row.id))}
                  onChange={() => toggleRow(Number(row.id))}
                  aria-label={`选择 ${String(row['登记编号'] ?? '')}`}
                />
              </td>
              {columns.map((column) => (
                <td key={column}>
                  {column === '登记状态' || column === 'status'
                    ? <span className={statusBadge(String(row[column] ?? ''))}>{String(row[column] ?? '—')}</span>
                    : (row[column] ?? '—')}
                </td>
              ))}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无调用来源管理数据</td>
            </tr>
          )}
        </tbody>
      </table>

      {batch && (
        <section className="panel">
          <h3>批次 {batch.批次编号} 结算结果（{batch.时区口径}，{batch.开始日期} ~ {batch.结束日期}）</h3>
          {batch.分组结果.map((group) => (
            <div key={`${group.应用名称}-${group.调用标识}`} className="group-block">
              <div className="group-head">
                <span className={statusBadge(group.来源状态)}>{group.来源状态}</span>
                <strong>{group.登记编号}</strong>
                <span>{group.应用名称} / {group.调用标识}</span>
                <span className={group.成功 ? 'text-ok' : 'text-err'}>
                  {group.成功 ? '本组成功' : `本组失败：${group.失败原因 ?? ''}`}
                </span>
                {group.已失效 && <span className="badge badge-revoked">已退出次日额度</span>}
                {group.跳过日期.length > 0 && (
                  <span className="settle-hint">断点跳过：{group.跳过日期.join('、')}</span>
                )}
              </div>
              <table className="data-table sub-table">
                <thead>
                  <tr>
                    <th>业务日期</th>
                    <th>请求量</th>
                    <th>配额消耗</th>
                    <th>剩余额度</th>
                    <th>明细条数</th>
                    <th>是否超限</th>
                    <th>配额结果说明</th>
                  </tr>
                </thead>
                <tbody>
                  {group.配额结果.map((item) => (
                    <tr key={item.业务日期}>
                      <td>{item.业务日期}</td>
                      <td>{item.请求量}</td>
                      <td className={item.配额消耗 === 0 ? 'text-muted' : 'text-strong'}>{item.配额消耗}</td>
                      <td>{item.剩余额度}</td>
                      <td>{item.明细条数}</td>
                      <td>{item.是否超限 ? '超限' : '正常'}</td>
                      <td>{item.说明}</td>
                    </tr>
                  ))}
                  {group.配额结果.length === 0 && (
                    <tr><td className="empty-state" colSpan={7}>该日期范围内没有新进入的请求，断点未推进</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          ))}
        </section>
      )}

      <section className="panel">
        <h3>断点水位（已完成位置 / 上一结算日 / 上一日配额水位）</h3>
        <table className="data-table sub-table">
          <thead>
            <tr>
              <th>登记编号</th>
              <th>应用名称</th>
              <th>调用标识</th>
              <th>已完成位置</th>
              <th>上一结算日</th>
              <th>上一日配额水位</th>
              <th>失效标记</th>
              <th>最后结算时间</th>
            </tr>
          </thead>
          <tbody>
            {checkpoints.map((cp) => (
              <tr key={cp.id}>
                <td>{cp.登记编号}</td>
                <td>{cp.应用名称}</td>
                <td>{cp.调用标识}</td>
                <td>{cp.已完成位置}</td>
                <td>{cp.上一结算日 || '—'}</td>
                <td>{cp.上一日配额水位}</td>
                <td>{cp.已失效 ? <span className="badge badge-revoked">已失效</span> : <span className="badge badge-ok">有效</span>}</td>
                <td>{cp.最后结算时间}</td>
              </tr>
            ))}
            {checkpoints.length === 0 && (
              <tr><td className="empty-state" colSpan={8}>暂无断点数据</td></tr>
            )}
          </tbody>
        </table>
      </section>

      <section className="panel">
        <h3>配额消耗台账（结合来源登记清单逐条核对）</h3>
        <table className="data-table sub-table">
          <thead>
            <tr>
              <th>登记编号</th>
              <th>业务日期</th>
              <th>来源状态</th>
              <th>日配额上限</th>
              <th>请求总量</th>
              <th>配额消耗</th>
              <th>剩余额度</th>
              <th>明细条数</th>
              <th>超限</th>
            </tr>
          </thead>
          <tbody>
            {ledger.map((item) => (
              <tr key={item.id}>
                <td>{item.登记编号}</td>
                <td>{item.业务日期}</td>
                <td><span className={statusBadge(item.来源状态)}>{item.来源状态}</span></td>
                <td>{item.日配额上限}</td>
                <td>{item.请求总量}</td>
                <td className={item.配额消耗 === 0 ? 'text-muted' : 'text-strong'}>{item.配额消耗}</td>
                <td>{item.剩余额度}</td>
                <td>{item.明细条数}</td>
                <td>{item.是否超限 ? '超限' : '正常'}</td>
              </tr>
            ))}
            {ledger.length === 0 && (
              <tr><td className="empty-state" colSpan={9}>暂无台账数据，发起批量结算后生成</td></tr>
            )}
          </tbody>
        </table>
      </section>

      <footer className="page-foot">
        <span>共 {total} 条调用来源管理记录，已勾选 {selected.size} 条</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
