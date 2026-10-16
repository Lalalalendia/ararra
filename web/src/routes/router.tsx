import { createBrowserRouter } from 'react-router-dom';
import { DashboardPage } from '../features/dashboard/DashboardPage';
import { BooksPage } from '../features/books/BooksPage';
import { PageLayout } from './Layout';
import { routePaths } from './paths';

export const router = createBrowserRouter([
  {
    element: <PageLayout />,
    children: [
      { path: routePaths.home, element: <DashboardPage /> },
      { path: routePaths.books, element: <BooksPage /> },
    ],
  },
]);
