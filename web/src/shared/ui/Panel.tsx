import type { PropsWithChildren } from 'react';

export function Panel({ children }: PropsWithChildren) {
  return <section>{children}</section>;
}
