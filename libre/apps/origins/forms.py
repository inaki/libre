from django.forms import ModelForm, widgets

from suit.widgets import AutosizedTextarea, EnclosedInput, NumberInput


class OriginForm(ModelForm):
    class Meta:
        widgets = {
            'description': AutosizedTextarea(attrs={'rows': 3, 'class': 'input-xxlarge'}),
        }


class OriginPathForm(OriginForm):
    class Meta:
        widgets = {
            'description': AutosizedTextarea(attrs={'rows': 3, 'class': 'input-xxlarge'}),
            'path': EnclosedInput(prepend='icon-folder-open', attrs={'class': 'input-xxlarge'}),
        }


class OriginUploadedFileForm(OriginForm):
    class Meta:
        widgets = {
            'description': AutosizedTextarea(attrs={'rows': 3, 'class': 'input-xxlarge'}),
            'path': EnclosedInput(prepend='icon-folder-open', attrs={'class': 'input-xxlarge'}),
        }
