import { NavLink, Outlet } from 'react-router-dom';
import { routePaths } from './paths';

export function PageLayout() {
  return (
    <div className="shell">
      <header className="header">
        <span className="brand">РидЭра <span>Переводы</span></span>
        <nav aria-label="Основная навигация">
          <NavLink to={routePaths.home} end>Обзор</NavLink>
          <NavLink to={routePaths.books}>Книги</NavLink>
          <NavLink to={routePaths.team}>Команда</NavLink>
        </nav>
      </header>
      <main className="content"><Outlet /></main>
    </div>
  );
}
