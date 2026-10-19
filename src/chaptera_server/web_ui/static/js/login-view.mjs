/** One key per team member. The key is exchanged once for a revocable
 * HttpOnly Secure cookie and is never saved in browser storage.
 */
export function installAuthView({auth,activateSession,clearWorkspace,
                                 flash,busyDuring,canLeave}) {
  const $=id=>document.getElementById(id);
  const loginForm=$('login-form');
  const workspace=$('authenticated');

  function showLogin(){
    document.title='Вход';
    document.body.dataset.authenticated='false';
    workspace.hidden=true;
    loginForm.hidden=false;
    loginForm.reset();
  }
  async function accept(session){
    if(session.auth_kind!=='team_key') {
      auth.clear();showLogin();
      throw new Error('Нужен персональный ключ участника.');
    }
    await activateSession(session);
  }
  loginForm.addEventListener('submit',event=>{
    event.preventDefault();
    void busyDuring(async()=>{
      const secret=String(new FormData(loginForm).get('api_key')||'').trim();
      $('login-secret').value='';
      try {await accept(await auth.login(secret));flash('');}
      catch(e){auth.clear();clearWorkspace();showLogin();flash(e.message,true);}
    });
  });
  $('logout').addEventListener('click',()=>{
    if(!canLeave())return;
    void busyDuring(async()=>{
      try {await auth.logout();}
      catch(e){flash(e.message,true);return;}
      clearWorkspace();showLogin();flash('');
    });
  });
  void busyDuring(async()=>{
    try {
      const session=await auth.restore();
      if(session)await accept(session);
    }catch(e){auth.clear();clearWorkspace();showLogin();flash('Не удалось восстановить вход: '+e.message,true);}
    finally{document.body.dataset.authReady='1';}
  });
}
