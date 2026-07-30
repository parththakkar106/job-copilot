import React, { createContext, useCallback, useContext, useState } from 'react'

const ToastCtx = createContext(() => {})
export const useToast = () => useContext(ToastCtx)

let seq = 0

export function ToastProvider({ children }) {
  const [items, setItems] = useState([])

  const notify = useCallback((message, kind = 'info', ttl = 4200) => {
    const id = ++seq
    setItems((xs) => [...xs, { id, message, kind }])
    if (ttl) setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== id)), ttl)
  }, [])

  return (
    <ToastCtx.Provider value={notify}>
      {children}
      <div className="toast-stack">
        {items.map((t) => (
          <div key={t.id} className={`toast toast-${t.kind}`}
               onClick={() => setItems((xs) => xs.filter((x) => x.id !== t.id))}>
            {t.message}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  )
}
