from app import inject_contact_icons, create_pdf

html = '''<div class="resume-wrapper">
    <table class="header-table">
        <tr>
            <td class="photo-cell"><img src="" class="profile-pic" /></td>
            <td class="info-cell">
                <h1>AMINE TAGHI</h1>
                <p class="contact-info">+212 615162643 | zdx.taghi@gmail.com | linkedin.com/in/aminetaghi | github.com/Rib4ko</p>
            </td>
        </tr>
    </table>
</div>'''

html = inject_contact_icons(html)
ok = create_pdf(html, 'static/resumes/test_resume.pdf')
print('PDF created:', ok)
