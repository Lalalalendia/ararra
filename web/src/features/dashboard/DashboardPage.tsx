import { Panel } from '../../shared/ui/Panel';
import { useEffect, useState } from 'react';
import { getHealth } from '../../shared/api/client';

type State = 'loading' | 'online' | 'offline';

export function DashboardPage() {
  const [state, setState] = useState<State>('loading');

  useEffect(() => {
    let active = true;
    getHealth().then(
      () => { if (active) setState('online'); },
      () => { if (active) setState('offline'); },
    );
    return () => { active = false; };
  }, []);

  const statusText = {
    loading: 'Проверка соединения с API',
    online: 'API отвечает',
    offline: 'API недоступен',
  }[state];

  return (
    <Panel>
      <p className="eyebrow">Серверный переводчик</p>
      <h1>Рабочее пространство команды</h1>
      <p>Основа веб-клиента. Импорт книг, библиотека и редактор добавляются отдельными модулями.</p>
      <p role="status" aria-live="polite" className="status">{statusText}</p>
    </Panel>
  );
}
