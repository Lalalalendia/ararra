import { NavLink, Outlet } from 'react-router-dom';
import { routePaths } from './paths';

export function PageLayout() {
  return <div className="shell">
    <aside className="app-sidebar">
      <div className="app-brand">
        <span className="app-brand-mark" aria-hidden="true">Р</span>
        <span className="brand">РидЭра <small>Переводческая платформа</small></span>
      </div>
      <p className="sidebar-label">РАБОЧЕЕ ПРОСТРАНСТВО</p>
      <nav aria-label="Основная навигация" className="app-nav">
        <NavLink to={routePaths.home} end>Обзор</NavLink>
        <NavLink to={routePaths.books}>Библиотека</NavLink>
        <NavLink to={routePaths.import}>Импорт книги</NavLink>
        <NavLink to={routePaths.team}>Команда</NavLink>
      </nav>
      <div className="sidebar-footer">Закрытая платформа команды
        <small>Доступ только по ключу участника</small>
      </div>
    </aside>
    <div className="app-main">
      <header className="header">
        <span>Рабочее пространство переводов</span>
        <span className="app-header-tag">Командная платформа</span>
      </header>
      <main className="content"><Outlet /></main>
    </div>
  </div>;
}
