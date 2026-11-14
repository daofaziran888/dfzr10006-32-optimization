import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/limiter'
const columns = [
  "规则编号",
  "限流对象",
  "限流算法",
  "每秒上限",
  "突发容量",
  "降级动作",
  "恢复时间",
  "规则状态"
]
// 跨日结算同步过来的配额水位字段，仅同步过的来源规则才有值。
const syncColumns = ["业务日期", "配额消耗", "剩余额度"]
const actions = [
  "启用限流",
  "触发降级",
  "关闭限流"
]
const statuses = [
  "草稿",
  "生效中",
  "已触发",
  "已关闭"
]

type Row = Record<string, string | number | null>

export default function LimiterView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const stats = useMemo(() => [
    { label: '限流规则总数', value: rows.length },
    { label: '生效中限流', value: rows.filter((row) => row.status === '生效中').length },
    { label: '已触发限流', value: rows.filter((row) => row.status === '已触发').length },
    { label: '已关闭（来源失效）', value: rows.filter((row) => row.status === '已关闭').length },
  ], [rows])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('限流规则列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '调用限流降级列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? '调用限流降级动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '调用限流降级操作失败')
    }
  }

  return (
    <section className="page" data-module="limiter">
      <header className="page-head">
        <div>
          <h2>调用限流降级管理</h2>
          <p className="page-desc">维护调用限流的算法选择、阈值设定与降级动作配置；跨日结算结果会按调用标识同步到此，超限触发降级、来源冻结/注销后规则立即关闭。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('限流规则登记入口尚未接入审批流')}>
            登记限流规则
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出调用限流降级清单
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

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            {syncColumns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => (
                <td key={column}>
                  {column === '规则状态' && row['来源登记编号']
                    ? <span className="badge badge-ok">{String(row[column] ?? '—')}</span>
                    : (row[column] ?? '—')}
                </td>
              ))}
              {syncColumns.map((column) => (
                <td key={column}>{row[column] ?? '—'}</td>
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
              <td className="empty-state" colSpan={columns.length + syncColumns.length + 1}>暂无调用限流降级数据，可先登记限流规则</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条调用限流降级记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
