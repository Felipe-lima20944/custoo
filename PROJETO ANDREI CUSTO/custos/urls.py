from django.urls import path
from . import views

urlpatterns = [
    # Rota para o upload de arquivos.
    path('', views.upload_file_view, name='upload_file'),
    
    # Rota para a análise dos dados. Note a adição de <uuid:file_id>
    # Esta parte do padrão diz ao Django para esperar um UUID na URL
    # e passá-lo para a view como a variável 'file_id'.
    path('analise/<uuid:file_id>/', views.analyze_data_view, name='analyze_data'),
    
    # Rota para a edição do nome do arquivo
    path('edit/<uuid:file_id>/', views.edit_file_name_view, name='edit_file_name'),
    
    # Rota para a exclusão do arquivo
    path('delete/<uuid:file_id>/', views.delete_file_view, name='delete_file'),
    
    # Rota para download do arquivo Excel
    path('download/<uuid:file_id>/', views.download_file_view, name='download_file_view'),
    
    # Rota para limpar a sessão (manter por compatibilidade)
    path('limpar-sessao/', views.clear_session_view, name='clear_session'),


    path('update_row_total/<uuid:file_id>/', views.update_row_total_view, name='update_row_total'),

]
