/** Closed-team key/password login screens; API calls only through session-client.
 * No credentials in storage, URLs, logs or Jinja templates.
 */
export function installAuthView({auth,activateSession,clearWorkspace,
                                 flash,busyDuring,canLeave}) {
  const $=id=>document.getElementById(id);
  const loginForm=$('login-form'),change=$('password-change');
  const workspace=$('authenticated');
  const openChange=$('change-password-open');
  let forced=false;
  let currentMember='';
  const loginMode=()=>loginForm.querySelector('[name="login_mode"]:checked')?.value||'key';

  function updateMode(){
    const usePassword=loginMode()==='password';
    $('login-member-row').hidden=!usePassword;
    $('login-member').required=usePassword;
    $('login-secret-label').textContent=usePassword?'Пароль модератора':'Ключ участника';
    $('login-secret').autocomplete=usePassword?'current-password':'off';
  }
  loginForm.querySelectorAll('[name="login_mode"]').forEach(el=>
    el.addEventListener('change',updateMode));
  updateMode();

  function showLogin(){
    forced=false;
    change.hidden=true;
    workspace.hidden=true;
    loginForm.hidden=false;
    loginForm.reset();
    $('password-change-form').reset();
    $('login-mode-key').checked=true;
    updateMode();
    openChange.hidden=true;
  }
  function showChange(member,required){
    currentMember=member;
    forced=required;
    loginForm.hidden=true;
    workspace.hidden=true;
    change.hidden=false;
    $('password-change-form').reset();
    $('password-change-help').textContent=required
      ? 'Первый пароль временный. Для доступа к книгам замените его собственным.'
      : 'Введите текущий пароль и новый пароль. После смены потребуется войти снова.';
    $('password-change-member').textContent=member;
    $('change-password-current').focus();
  }
  async function accept(session) {
    if(session.must_change_password) {
      showChange(session.member_id,true);
    } else {
      change.hidden=true;
      await activateSession(session);
      currentMember=session.member_id;
      openChange.hidden=session.auth_kind!=='password';
    }
  }
  loginForm.addEventListener('submit',event=>{
    event.preventDefault();
    void busyDuring(async()=>{
      const data=new FormData(loginForm);
      const secret=String(data.get('api_key')||'');
      const member=String(data.get('member_id')||'').trim();
      const isPassword=loginMode()==='password';
      loginForm.querySelector('[name="api_key"]').value='';
      try {
        const session=isPassword
          ? await auth.loginPassword(member,secret)
          : await auth.login(secret.trim());
        await accept(session);
        flash('');
      } catch(e) {auth.clear();flash(e.message,true);}
    });
  });
  async function signOut() {
    if(!canLeave())return;
    await busyDuring(async()=>{
      try {await auth.logout();}
      catch(e){flash(e.message,true);return;}
      clearWorkspace();showLogin();flash('');
    });
  }
  $('logout').addEventListener('click',()=>void signOut());
  openChange.addEventListener('click',()=>{
    if(canLeave())showChange(currentMember,false);
  });
  $('password-change-cancel').addEventListener('click',()=>{
    if(forced){void signOut();return;}
    change.hidden=true;workspace.hidden=false;$('password-change-form').reset();
  });
  $('password-change-form').addEventListener('submit',event=>{
    event.preventDefault();
    void busyDuring(async()=>{
      const data=new FormData(event.currentTarget);
      const old=String(data.get('current_password')||'');
      const next=String(data.get('new_password')||'');
      if(next!==String(data.get('confirmation')||'')){
        flash('Новый пароль и подтверждение не совпадают.',true);return;
      }
      try {
        await auth.changePassword(old,next);
        clearWorkspace();showLogin();
        flash('Пароль изменён, все прежние парольные сессии отозваны. Войдите снова.');
      } catch(e){flash(e.message,true);}
    });
  });
  void busyDuring(async()=>{
    try {
      const session=await auth.restore();
      if(session)await accept(session);
    } catch(e){flash('Не удалось восстановить вход: '+e.message,true);}
    finally{document.body.dataset.authReady='1';}
  });
}
