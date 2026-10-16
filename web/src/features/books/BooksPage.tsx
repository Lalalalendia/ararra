import { Panel } from '../../shared/ui/Panel';
export function BooksPage() {
  return (
    <Panel>
      <p className="eyebrow">Библиотека</p>
      <h1>Книги</h1>
      <p>Список книг и импорт будут подключены к защищённому API на следующем этапе.</p>
      <p className="status">Содержимое книг не загружается без командной авторизации.</p>
    </Panel>
  );
}
