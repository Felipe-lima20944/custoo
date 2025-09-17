from django import forms

class UploadArquivoForm(forms.Form):
    """
    Formulário para upload de arquivo Excel.
    """
    name = forms.CharField(label='Nome da Análise (opcional)', max_length=255, required=False)
    arquivo_excel = forms.FileField(label='Selecione o arquivo Excel (.xlsx)')
